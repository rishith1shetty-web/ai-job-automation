"""Phase 4 tests: Dice dry-run apply via the shared ScreeningAnswerEngine.

The apply flow is exercised without a real browser by overriding the three
interaction seams (`_apply_browser_context`, `_detect_apply_form`, `_fill_fields`).

Safety: a DRY RUN never submits. Live apply (dry_run=False) is now enabled and does submit,
but only after every safety gate passes -- see tests/test_dice_live_apply.py, which pins the
submit/confirm contract. No test here or there performs a real submission.
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.database import init_db
from app.models import ApplicationStatus, CandidateProfile, JobOpportunity, ResumeProfile
from app.services.portals.dice_portal import DicePortalAdapter


class FakePage:
    def __init__(self):
        self.submitted = False  # never set by the adapter -> proves no submission

    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def evaluate(self, *a, **k):
        return []


class FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class FakeDice(DicePortalAdapter):
    """DicePortalAdapter with the browser/DOM seams faked."""

    def __init__(self, settings, form):
        super().__init__(settings)
        self._form = form
        self.page = FakePage()
        self.filled_plan = None

    from contextlib import contextmanager as _cm

    @_cm
    def _apply_browser_context(self):
        yield FakeContext(self.page)

    def _detect_apply_form(self, page, job):
        return self._form

    def _fill_fields(self, page, plan):
        # Record the fill plan; crucially, this never triggers a submit.
        self.filled_plan = plan


def make_resume(**overrides):
    data = dict(
        filename="resume.txt",
        stored_path="resume.txt",
        raw_text="Data analyst with SQL and Excel.",
        skills=["SQL", "Excel"],
        technologies=[],
        education=[],
        experience=[],
        job_titles=[],
        keywords=[],
    )
    data.update(overrides)
    return ResumeProfile(**data)


def make_job():
    return JobOpportunity(
        resume_id=1, title="Data Analyst", company="Example",
        location="United States", job_url="https://www.dice.com/job-detail/abc",
    )


def make_candidate():
    return CandidateProfile(
        first_name="Ava", last_name="Shah", email="ava@example.com",
        mobile_phone="5551234567", current_location="New York, NY",
    )


class DiceApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()  # GeneralizedFormEngine.detect_field may touch learning tables

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.resume = make_resume()
        self.job = make_job()

    def _apply(self, form, candidate=None, context=None, dry_run=True):
        adapter = FakeDice(self.settings, form)
        result = adapter.apply_to_job(
            self.job, self.resume, candidate,
            dry_run=dry_run, capture_evidence=False,
            screening_context=context or {"mode": "safe", "saved_answers": []},
        )
        return adapter, result

    def test_live_apply_fills_and_reaches_the_submit_stage(self):
        """Live apply is now ENABLED (it used to hard-stop at `dice_live_apply_disabled`).

        This FakePage exposes no Submit control, so the adapter must report that honestly
        instead of claiming success -- and it must still never mark anything submitted.
        """
        adapter, result = self._apply({"apply_type": "native", "fields": []}, dry_run=False)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(result.current_step, "dice_submit_not_found")
        self.assertFalse(adapter.page.submitted)
        self.assertIsNotNone(adapter.filled_plan)      # live mode DOES fill the form
        self.assertNotEqual(result.status, ApplicationStatus.DRY_RUN_READY)

    def test_dry_run_never_submits_even_though_live_apply_exists(self):
        adapter, result = self._apply({"apply_type": "native", "fields": []}, dry_run=True)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(adapter.page.submitted)

    def test_external_form_is_unsupported(self):
        _, result = self._apply({"apply_type": "external", "fields": []})
        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)

    def test_no_form_is_unsupported(self):
        _, result = self._apply({"apply_type": "none", "fields": []})
        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)

    def test_dry_run_ready_when_all_fields_answerable(self):
        engine = DicePortalAdapter(self.settings).screening
        saved = {
            "normalized_question": engine.normalize_question("Do you have SQL experience?"),
            "answer": "Yes", "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "skill", "evidence": {},
        }
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []},
                {"index": 1, "label": "Email", "control": "text", "kind": "text", "required": True, "options": []},
                {"index": 2, "label": "Do you have SQL experience?", "control": "radio", "kind": "radio", "required": True, "options": ["Yes", "No"]},
            ],
        }
        adapter, result = self._apply(
            form, candidate=make_candidate(),
            context={"mode": "safe", "saved_answers": [saved]},
        )
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(result.current_step, "dice_dry_run_ready")
        # Filled the form but never submitted.
        self.assertIsNotNone(adapter.filled_plan)
        self.assertFalse(adapter.page.submitted)
        # Screening decision came from the shared engine and is recorded.
        self.assertTrue(any(e.get("selected_answer") == "Yes" for e in result.screening_answers))

    def test_needs_review_when_required_screening_uncertain(self):
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "Are you legally authorized to work in the United States?",
                 "control": "radio", "kind": "radio", "required": True, "options": ["Yes", "No"]},
            ],
        }
        adapter, result = self._apply(form, candidate=make_candidate())
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(result.current_step, "dice_apply_needs_review")
        self.assertFalse(adapter.page.submitted)

    def test_needs_review_when_required_contact_missing(self):
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []},
            ],
        }
        # No candidate profile -> required contact field cannot be filled.
        adapter, result = self._apply(form, candidate=None)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertFalse(adapter.page.submitted)

    def test_dry_run_ready_covers_number_and_dropdown_fields(self):
        # Resume carries explicit years + skills so number/dropdown fields are
        # answerable from evidence via the upgraded shared engine.
        self.resume = make_resume(
            raw_text="Data analyst with 5 years of experience in SQL and Tableau.",
            skills=["SQL", "Tableau"],
        )
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []},
                {"index": 1, "label": "Email", "control": "text", "kind": "text", "required": True, "options": []},
                {"index": 2, "label": "How many years of experience do you have?", "control": "number", "kind": "number", "required": True, "options": []},
                {"index": 3, "label": "Which of these have you used?", "control": "select", "kind": "select", "required": True, "options": ["SQL", "MongoDB", "Redis"]},
            ],
        }
        adapter, result = self._apply(form, candidate=make_candidate())
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(adapter.page.submitted)
        answers = {e.get("question_text"): e.get("selected_answer") for e in result.screening_answers}
        self.assertEqual(answers.get("How many years of experience do you have?"), "5")
        self.assertEqual(answers.get("Which of these have you used?"), "SQL")

    def test_number_field_without_evidence_fills_zero(self):
        # No explicit years -> numeric field is filled with an honest 0 (AI-generated)
        # so the form is submit-ready; the value is always numeric, never "Yes".
        self.resume = make_resume(raw_text="Data analyst.", skills=[])
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "How many years of experience do you have?", "control": "number", "kind": "number", "required": True, "options": []},
            ],
        }
        adapter, result = self._apply(form, candidate=make_candidate())
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(adapter.page.submitted)
        answers = {e.get("question_text"): e.get("selected_answer") for e in result.screening_answers}
        self.assertEqual(answers.get("How many years of experience do you have?"), "0")

    def test_result_shape_is_automationresult_compatible(self):
        _, result = self._apply({"apply_type": "external", "fields": []})
        # Fields consumed by main._process_application must exist.
        for attr in ("status", "message", "current_step", "evidence_path", "log", "screening_answers", "missing_contact_fields"):
            self.assertTrue(hasattr(result, attr))


class FakeLocator:
    """Minimal Playwright Locator double: `.first` returns itself, `.count()`
    reflects whether this selector/role query matched, and `.click()` runs an
    optional callback so fakes can simulate a modal opening or a step advancing."""

    def __init__(self, matched: bool, on_click=None):
        self._matched = matched
        self._on_click = on_click

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._matched else 0

    def is_visible(self):
        return self._matched

    def is_enabled(self):
        return self._matched

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def click(self, timeout=None):
        if self._on_click:
            self._on_click()


class CityAutocompleteFake:
    """Mixin giving a fake page a live-like 'Current city of residence' autocomplete:
    typing opens a suggestion listbox, and the value only commits when a suggestion
    is clicked. (CityInputLocator / CitySuggestionLocator are defined below and
    resolved at call time.)"""

    def _init_city(self, city_suggestions=None, commit_on_suggestion_click=True):
        # city_suggestions=None -> derive from what is typed, like a real autocomplete
        # answering the query. Pass an explicit list to pin the results, or [] to
        # simulate "no suggestions".
        self._fixed_city_suggestions = None if city_suggestions is None else list(city_suggestions)
        self.city_suggestions: list = []
        self.city_suggestions_open = False
        self.city_suggestion_clicks = 0
        self.city_text = None
        self.city_committed_value = ""
        self.commit_on_suggestion_click = commit_on_suggestion_click

    def _suggestions_for(self, typed: str) -> list:
        if self._fixed_city_suggestions is not None:
            return list(self._fixed_city_suggestions)
        return [f"{typed}, USA"] if typed else []

    def _city_locator(self, selector, city_input_present: bool):
        sel = selector.lower()
        if sel.startswith("input[") and ("city" in sel or "location" in sel):
            return CityInputLocator(self) if city_input_present else FakeLocator(False)
        if any(token in sel for token in ("option", "listbox", "autocomplete", "suggestion")):
            return CitySuggestionLocator(self)
        return None


class HrefLocator(FakeLocator):
    """A matched Locator that also exposes an href, like the real Dice apply CTA
    (`<a data-testid='apply-button' href='/job-applications/<id>/start-apply'>`),
    so the adapter can navigate to the wizard directly instead of clicking."""

    def __init__(self, href: str, on_click=None):
        super().__init__(True, on_click=on_click)
        self._href = href

    def get_attribute(self, name, timeout=None):
        return self._href if name == "href" else None


class RealDetectionDice(DicePortalAdapter):
    """DicePortalAdapter with only the browser-launch seam faked; `_detect_apply_form`
    and its helpers (`_open_native_apply`, `_has_external_apply`, `_collect_fields`,
    `_advance_to_next_step`, `_fill_fields`) run for real against a fake `page`."""

    def __init__(self, settings, page):
        super().__init__(settings)
        self.page = page

    from contextlib import contextmanager as _cm

    @_cm
    def _apply_browser_context(self):
        yield FakeContext(self.page)


class BareApplyButtonPage:
    """A real Dice job page's only apply control is a plain 'Apply' button (no
    'Easy'/'now' suffix, no data-cy hook) -- confirmed from a live debug screenshot
    of a real apply attempt (outputs/dice_debug/.../job_25831/apply_form.png) where
    the legacy 'Easy apply'/'Apply now'/data-cy selectors never matched it.
    """

    def __init__(self):
        self.apply_opened = False
        self.submitted = False
        self.filled_plan = None

    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html><button>Apply</button></html>"

    def locator(self, selector):
        # None of the legacy text/data-cy selectors match a bare "Apply" button.
        return FakeLocator(False)

    def get_by_role(self, role, name):
        matched = role == "button" and bool(name.search("Apply"))
        return FakeLocator(matched, on_click=lambda: setattr(self, "apply_opened", True))

    def evaluate(self, script, *args):
        if args:
            self.filled_plan = args[0]
            return True
        if self.apply_opened:
            return [{"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []}]
        return []


class BareApplyLinkPage:
    """Same real-world gap as BareApplyButtonPage, but the CTA is an anchor
    (`role="link"`) rather than a button -- exercises the role fallback loop."""

    def __init__(self):
        self.apply_opened = False
        self.submitted = False
        self.filled_plan = None

    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html><a>Apply</a></html>"

    def locator(self, selector):
        return FakeLocator(False)

    def get_by_role(self, role, name):
        matched = role == "link" and bool(name.search("Apply"))
        return FakeLocator(matched, on_click=lambda: setattr(self, "apply_opened", True))

    def evaluate(self, script, *args):
        if args:
            self.filled_plan = args[0]
            return True
        if self.apply_opened:
            return [{"index": 0, "label": "Email", "control": "text", "kind": "text", "required": True, "options": []}]
        return []


class OverlayThenApplyPage:
    """A cookie/consent overlay covers the page until dismissed; the Apply
    control only becomes matchable afterward -- exercises overlay dismissal
    running before the apply-button search."""

    def __init__(self):
        self.overlay_present = True
        self.apply_opened = False
        self.submitted = False
        self.filled_plan = None

    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html></html>"

    def locator(self, selector):
        if selector == "[aria-label='Close' i]" and self.overlay_present:
            def dismiss():
                self.overlay_present = False
            return FakeLocator(True, on_click=dismiss)
        return FakeLocator(False)

    def get_by_role(self, role, name):
        matched = role == "button" and not self.overlay_present and bool(name.search("Apply"))
        return FakeLocator(matched, on_click=lambda: setattr(self, "apply_opened", True))

    def evaluate(self, script, *args):
        if args:
            self.filled_plan = args[0]
            return True
        if self.apply_opened:
            return [{"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []}]
        return []


class MultiStepApplyPage:
    """A bare 'Apply' button opens a two-step form; step 2's fields only
    appear after clicking 'Next'. Never exposes a 'Submit' control, so the
    never-submit invariant is trivially provable: nothing in this fake can
    be clicked to submit."""

    def __init__(self):
        self.apply_opened = False
        self.step = 0
        self.next_clicked = False
        self.submitted = False
        self.filled_plan = None

    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html></html>"

    def locator(self, selector):
        if selector == "button:has-text('Next')" and self.apply_opened and self.step == 1:
            def advance():
                self.step = 2
                self.next_clicked = True
            return FakeLocator(True, on_click=advance)
        return FakeLocator(False)

    def get_by_role(self, role, name):
        matched = role == "button" and bool(name.search("Apply")) and not self.apply_opened

        def open_apply():
            self.apply_opened = True
            self.step = 1

        return FakeLocator(matched, on_click=open_apply)

    def evaluate(self, script, *args):
        if args:
            self.filled_plan = args[0]
            return True
        if self.step == 1:
            return [{"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []}]
        if self.step == 2:
            return [
                {"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []},
                {"index": 1, "label": "Email", "control": "text", "kind": "text", "required": True, "options": []},
            ]
        return []


class FakeContextWithPopupSupport:
    """Minimal Playwright BrowserContext double supporting the "page" event
    used to detect a popup opened by clicking Apply."""

    def __init__(self):
        self._listeners: dict[str, list] = {}

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self._listeners.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit(self, event, *args):
        for handler in list(self._listeners.get(event, [])):
            handler(*args)


class PopupPage:
    """The popup opened by clicking Apply; the real form lives here, not on
    the original job-listing page."""

    def __init__(self):
        self.filled_plan = None
        self.submitted = False

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html></html>"

    def locator(self, selector):
        return FakeLocator(False)

    def evaluate(self, script, *args):
        if args:
            self.filled_plan = args[0]
            return True
        return [{"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []}]


class PopupApplyPage:
    """Simulates Dice routing the apply flow through a NEW TAB rather than an
    in-page modal: clicking the bare 'Apply' button fires a Playwright 'page'
    event on the context with a separate popup page, and only the popup (never
    this original page) exposes any form fields -- proving the adapter must
    switch to it rather than keep polling the original page."""

    def __init__(self):
        self.context = FakeContextWithPopupSupport()
        self.spawned_popup: PopupPage | None = None
        self.submitted = False

    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html></html>"

    def locator(self, selector):
        return FakeLocator(False)

    def get_by_role(self, role, name):
        matched = role == "button" and bool(name.search("Apply"))

        def open_popup():
            self.spawned_popup = PopupPage()
            self.context.emit("page", self.spawned_popup)

        return FakeLocator(matched, on_click=open_popup)

    def evaluate(self, script, *args):
        # This original page never has any fields -- proves the adapter must
        # use the popup, not this page, to find and fill them.
        if args:
            return True
        return []


class DiceRealFormDetectionTests(unittest.TestCase):
    """Exercises the real `_open_native_apply` / `_has_external_apply` /
    `_collect_fields_script` / `_advance_to_next_step` DOM logic (previously
    only ever bypassed via a full `_detect_apply_form` override in
    DiceApplyTests), against fake pages shaped like real Dice markup.
    """

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.resume = make_resume()
        self.job = make_job()

    def _apply(self, page, candidate=None):
        adapter = RealDetectionDice(self.settings, page)
        result = adapter.apply_to_job(
            self.job, self.resume, candidate,
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": []},
        )
        return adapter, result

    def test_bare_apply_button_is_found_and_form_is_filled(self):
        page = BareApplyButtonPage()
        _, result = self._apply(page, candidate=make_candidate())
        self.assertTrue(page.apply_opened, "the bare 'Apply' button should have been clicked")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertIsNotNone(page.filled_plan)
        self.assertFalse(page.submitted)

    def test_bare_apply_link_is_found_via_role_fallback(self):
        page = BareApplyLinkPage()
        _, result = self._apply(page, candidate=make_candidate())
        self.assertTrue(page.apply_opened, "the bare 'Apply' link should have been clicked")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_overlay_is_dismissed_before_apply_button_search(self):
        page = OverlayThenApplyPage()
        _, result = self._apply(page, candidate=make_candidate())
        self.assertFalse(page.overlay_present, "overlay should have been dismissed")
        self.assertTrue(page.apply_opened)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_multi_step_next_advances_without_submitting(self):
        page = MultiStepApplyPage()
        _, result = self._apply(page, candidate=make_candidate())
        self.assertTrue(page.next_clicked, "Next should have been clicked to reveal step 2 fields")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertIsNotNone(page.filled_plan)
        self.assertFalse(page.submitted)

    def test_apply_opening_new_tab_switches_to_popup_and_fills_it(self):
        # Dice sometimes opens the application in a new tab instead of an
        # in-page modal. The original page never exposes form fields, so the
        # adapter must switch to the popup to find and fill them.
        page = PopupApplyPage()
        _, result = self._apply(page, candidate=make_candidate())
        self.assertIsNotNone(page.spawned_popup, "clicking Apply should have opened a popup tab")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        # Fields were filled on the popup, never on the original page.
        self.assertIsNotNone(page.spawned_popup.filled_plan)
        self.assertFalse(page.submitted)
        self.assertFalse(page.spawned_popup.submitted)


class WizardWaitPage:
    """Reproduces the REAL Dice Easy Apply flow that regressed:

    The apply CTA is an <a data-testid='apply-button'> whose href points at
    /job-applications/<id>/start-apply. Activating it navigates the SAME tab to a
    wizard URL, and the wizard's fields only render after a few polls (delayed
    client-side render). Proves the adapter (a) reaches the wizard URL, (b) waits
    through the delayed render instead of closing after ~2s, and (c) never submits.
    """

    START_URL = "https://www.dice.com/job-detail/abc"
    APPLY_HREF = "/job-applications/9da9-1234/start-apply"
    WIZARD_URL = "https://www.dice.com/job-applications/9da9-1234/wizard"

    def __init__(self, field_delay_polls: int = 3):
        self.url = self.START_URL
        self.field_delay_polls = field_delay_polls
        self.polls = 0
        self.navigated_to: list[str] = []
        self.submitted = False
        self.filled_plan = None
        self.context = None  # same-tab navigation, no popup

    def goto(self, url, **k):
        self.navigated_to.append(url)
        # start-apply immediately redirects to the wizard.
        self.url = self.WIZARD_URL
        return None

    def wait_for_url(self, *a, **k):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_selector(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        self.polls += 1
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html><!-- wizard --></html>"

    def locator(self, selector):
        if selector == "[data-testid='apply-button']":
            return HrefLocator(self.APPLY_HREF)
        return FakeLocator(False)

    def get_by_role(self, role, name):
        return FakeLocator(False)

    def evaluate(self, script, *args):
        if args:
            self.filled_plan = args[0]
            return True
        # Fields appear only on the wizard URL AND after the delayed render.
        if "/job-applications/" in self.url and self.polls >= self.field_delay_polls:
            return [{"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []}]
        return []


class WizardNoFieldsPage(WizardWaitPage):
    """The wizard URL is reached but fields never render (unknown DOM / slow).
    Must classify as unsupported_form with a wizard-specific reason, and save
    evidence -- NOT a false dry_run_ready and NOT close instantly as 'no form'."""

    def __init__(self):
        super().__init__(field_delay_polls=10**9)  # fields never appear
        self.content_calls = 0

    def content(self):
        self.content_calls += 1
        return "<html><!-- wizard, no readable fields --></html>"


class DiceWizardFlowTests(unittest.TestCase):
    """Exercises the real same-tab wizard navigation + wait logic."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.resume = make_resume()
        self.job = make_job()

    def _apply(self, page, candidate=None, capture_evidence=False):
        adapter = RealDetectionDice(self.settings, page)
        # Keep the poll budget small so a never-loading wizard test stays fast.
        adapter._apply_panel_max_polls = 12
        result = adapter.apply_to_job(
            self.job, self.resume, candidate,
            dry_run=True, capture_evidence=capture_evidence,
            screening_context={"mode": "safe", "saved_answers": []},
        )
        return adapter, result

    def _wizard_wait_step(self, result):
        return next((s for s in result.log if s.get("step") == "wizard_wait"), None)

    def test_easy_apply_navigates_to_wizard_url(self):
        page = WizardWaitPage()
        _, result = self._apply(page, candidate=make_candidate())
        # The adapter navigated to the apply href (start-apply -> wizard).
        self.assertTrue(any("start-apply" in url for url in page.navigated_to))
        self.assertEqual(page.url, WizardWaitPage.WIZARD_URL)
        wait = self._wizard_wait_step(result)
        self.assertIsNotNone(wait)
        self.assertTrue(wait["wizard_detected"])

    def test_automation_waits_for_delayed_wizard_fields(self):
        # Fields only appear on the 3rd poll; the adapter must keep polling.
        page = WizardWaitPage(field_delay_polls=3)
        _, result = self._apply(page, candidate=make_candidate())
        self.assertGreaterEqual(page.polls, 3, "adapter closed before the wizard rendered its fields")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        wait = self._wizard_wait_step(result)
        self.assertTrue(wait["wizard_loaded"])
        self.assertGreaterEqual(wait["fields_found"], 1)

    def test_wizard_flow_reaches_dry_run_ready_and_never_submits(self):
        page = WizardWaitPage()
        _, result = self._apply(page, candidate=make_candidate())
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(result.current_step, "dice_dry_run_ready")
        self.assertIsNotNone(page.filled_plan)   # form was filled...
        self.assertFalse(page.submitted)          # ...but never submitted

    def test_wizard_opened_but_no_fields_is_unsupported_with_reason(self):
        page = WizardNoFieldsPage()
        _, result = self._apply(page, candidate=make_candidate(), capture_evidence=True)
        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)
        self.assertEqual(result.current_step, "dice_apply_unsupported_form")
        self.assertIn("wizard", result.message.lower())
        # Evidence (page HTML) was captured before giving up.
        self.assertGreaterEqual(page.content_calls, 1)
        self.assertFalse(page.submitted)

    def test_does_not_close_early_when_url_not_yet_wizard(self):
        # If fields render only after navigation completes, an early read returns
        # nothing; the adapter must still end up loading the wizard (not bail).
        page = WizardWaitPage(field_delay_polls=5)
        _, result = self._apply(page, candidate=make_candidate())
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)


class FileInputLocator(FakeLocator):
    """A matched <input type=file> locator that records set_input_files payloads
    and, after upload, reports el.files.length so upload confirmation can pass."""

    def __init__(self, page, matched: bool):
        super().__init__(matched)
        self._page = page

    def set_input_files(self, files, timeout=None):
        self._page.uploaded_files.append(files)
        self._page.uploaded_count += 1

    def evaluate(self, script, *a, **k):
        # Simulate `el.files.length` for _confirm_resume_upload.
        return self._page.uploaded_count if self._page.confirm_uploads else 0


class ScriptedWizardPage(CityAutocompleteFake):
    """Fake Dice Easy Apply wizard driven by a scripted list of steps (each a list
    of field dicts). Reproduces the real flow (Step 1 Resume & Cover Letter -- pure
    navigation -- then Additional Information + Next, ... final step with Submit).
    Records any file uploads (there must be none) and JS fills, exposes an Apply
    control so the adapter enters the wizard, and NEVER lets Submit be clicked
    (there is no code path here that sets submitted=True)."""

    WIZARD_URL = "https://www.dice.com/job-applications/abc-123/wizard"

    def __init__(self, steps, confirm_uploads: bool = True):
        self.steps = steps
        self.step_index = 0
        self.url = self.WIZARD_URL
        self.uploaded_files: list = []
        self.uploaded_count = 0
        self.confirm_uploads = confirm_uploads
        self.filled: list = []
        self.submitted = False
        self.next_clicks = 0
        self._init_city()

    # --- misc page API ---
    def goto(self, *a, **k):
        return None

    def wait_for_url(self, *a, **k):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_selector(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html><!-- dice wizard --></html>"

    # --- step helpers ---
    def _current_fields(self):
        return self.steps[self.step_index] if self.step_index < len(self.steps) else []

    def _is_last_step(self):
        return self.step_index >= len(self.steps) - 1

    def _advance(self):
        self.next_clicks += 1
        if not self._is_last_step():
            self.step_index += 1

    # --- locators ---
    def _has_city_field(self):
        return any("city" in str(f.get("label") or "").lower() for f in self._current_fields())

    def locator(self, selector):
        sel = selector.lower()
        if "type='file'" in sel or 'type="file"' in sel:
            has_file = any(f.get("kind") == "file" for f in self._current_fields())
            return FileInputLocator(self, has_file)
        # "next" is checked BEFORE "submit": the real Dice Next control is
        # <button type="submit">Next</button>, so its selector contains both words.
        if "next" in sel or "continue" in sel:
            return FakeLocator(not self._is_last_step(), on_click=self._advance)
        if "submit" in sel:
            return FakeLocator(self._is_last_step())
        city = self._city_locator(selector, self._has_city_field())
        if city is not None:
            return city
        return FakeLocator(False)

    def get_by_role(self, role, name):
        # The Apply control that opens the wizard (already at wizard URL). Only
        # matches an Apply-named query so the Next role fallback stays honest.
        return FakeLocator(role == "button" and bool(name.search("Apply")))

    def evaluate(self, script, *args):
        if args:
            self.filled.append(args[0])
            return True
        return list(self._current_fields())


class ResumeOnlyNoNextPage:
    """Reproduces the reported bug: after a Resume upload step there is NO Next and
    NO Submit control. The runner must NOT treat this as dry_run_ready (a resume
    step is never terminal) -- it must stop with a recorded 'next_not_found' reason
    -> unsupported_form, after the resume was uploaded."""

    WIZARD_URL = "https://www.dice.com/job-applications/x/wizard"

    def __init__(self, confirm_uploads: bool = True):
        self.url = self.WIZARD_URL
        self.uploaded_files: list = []
        self.uploaded_count = 0
        self.confirm_uploads = confirm_uploads
        self.submitted = False
        self.next_clicks = 0

    def goto(self, *a, **k):
        return None

    def wait_for_url(self, *a, **k):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_selector(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html><!-- resume only, no next --></html>"

    def locator(self, selector):
        sel = selector.lower()
        if "type='file'" in sel or 'type="file"' in sel:
            return FileInputLocator(self, True)
        return FakeLocator(False)  # no Next, no Submit, no overlay

    def get_by_role(self, role, name):
        # Only the Apply CTA matches by role; there is genuinely no Next control.
        return FakeLocator(role == "button" and bool(name.search("Apply")))

    def evaluate(self, script, *args):
        if args:
            return True
        return [_resume_file_field()]


def _resume_file_field():
    return {"index": 0, "label": "Resume", "control": "file", "kind": "file", "required": True, "options": []}


def _cover_letter_field():
    return {"index": 1, "label": "Cover letter", "control": "file", "kind": "file", "required": False, "options": []}


def _work_auth_field():
    return {"index": 0, "label": "Work Authorization", "control": "select", "kind": "select",
            "required": True, "options": ["US Citizen", "Green Card", "H1B", "Not authorized"]}


def _city_field():
    return {"index": 1, "label": "Current city of residence", "control": "text", "kind": "text",
            "required": True, "options": []}


class DiceWizardFillTests(unittest.TestCase):
    """End-to-end dry-run fill of the real 2-step Dice wizard: resume upload,
    cover-letter skip, work authorization (saved-answer only), city, stop before
    submit."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.job = make_job()
        # A real resume file on disk so set_input_files has something to upload.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._tmp.write(b"%PDF-1.4 fake resume")
        self._tmp.close()
        self.resume = make_resume(stored_path=self._tmp.name, filename="resume.pdf")

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _saved_work_auth(self, answer="US Citizen"):
        engine = DicePortalAdapter(self.settings).screening
        return {
            "normalized_question": engine.normalize_question("Work Authorization"),
            "answer": answer, "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "work_authorization", "evidence": {},
        }

    def _apply_page(self, page, candidate=None, saved_answers=None):
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._resume_confirm_max_polls = 3
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        result = adapter.apply_to_job(
            self.job, self.resume, candidate,
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": saved_answers or []},
        )
        return page, result

    def _apply(self, steps, candidate=None, saved_answers=None, confirm_uploads=True):
        page = ScriptedWizardPage(steps, confirm_uploads=confirm_uploads)
        return self._apply_page(page, candidate=candidate, saved_answers=saved_answers)

    def _wizard_steps_opened(self, result):
        return [s.get("wizard_step") for s in result.log if s.get("step") == "wizard_step_opened"]

    def _filled_labels(self, page):
        return [e.get("label") for plan in page.filled for e in plan]

    def _log_steps(self, result):
        return [s.get("step") for s in result.log]

    def test_step1_uploads_nothing_at_all(self):
        # Step 1 is navigation-only: no resume upload, no cover-letter upload,
        # no file interaction of any kind.
        steps = [[_resume_file_field(), _cover_letter_field()], []]  # step 2 = review/submit
        page, result = self._apply(steps, candidate=make_candidate(),
                                   saved_answers=[self._saved_work_auth()])
        self.assertEqual(page.uploaded_files, [], "Step 1 must not upload anything")
        steps_seen = self._log_steps(result)
        self.assertIn("step1_entered", steps_seen)
        self.assertIn("step1_completed", steps_seen)
        self.assertGreaterEqual(page.next_clicks, 1)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_work_authorization_uses_saved_answer(self):
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(),
                                   saved_answers=[self._saved_work_auth("US Citizen")])
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertTrue(any(e.get("selected_answer") == "US Citizen" for e in result.screening_answers))
        self.assertFalse(page.submitted)

    def test_missing_work_authorization_falls_back_to_configured_default(self):
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        # No saved answer -> the CONFIGURED default is used (never invented).
        page, result = self._apply(steps, candidate=make_candidate(), saved_answers=[])
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        selected = [s for s in result.log if s.get("step") == "work_authorization_selected"]
        self.assertEqual(selected[0]["value"], "US Citizen")
        self.assertEqual(selected[0]["source"], "configured_default")
        self.assertEqual(page.uploaded_files, [])
        self.assertFalse(page.submitted)

    def test_no_saved_answer_and_no_configured_default_needs_review(self):
        # With the configured default cleared, work authorization must NOT be invented.
        self.settings = Settings(openai_api_key=None, dice_default_work_authorization=None)
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(), saved_answers=[])
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(result.current_step, "dice_apply_needs_review")
        self.assertEqual(page.uploaded_files, [])
        self.assertFalse(page.submitted)

    def test_city_filled_from_profile(self):
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(),  # current_location = New York, NY
                                   saved_answers=[self._saved_work_auth()])
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(page.city_committed_value, "New York, NY, USA")  # committed via autocomplete
        self.assertFalse(page.submitted)

    def test_city_filled_from_resume_when_profile_missing(self):
        self.resume = make_resume(stored_path=self._tmp.name, filename="resume.pdf",
                                  raw_text="Senior Data Analyst based in Austin, TX with SQL and Python.")
        candidate = CandidateProfile(first_name="Ada", last_name="Byron", email="ada@example.com",
                                     mobile_phone="5551112222", current_location=None)
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=candidate,
                                   saved_answers=[self._saved_work_auth()])
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(page.city_committed_value, "Austin, TX, USA")  # committed via autocomplete
        self.assertFalse(page.submitted)

    def test_missing_city_needs_review(self):
        self.resume = make_resume(stored_path=self._tmp.name, filename="resume.pdf",
                                  raw_text="Data Analyst with SQL and Python.")  # no City, ST
        candidate = CandidateProfile(first_name="Ada", last_name="Byron", email="ada@example.com",
                                     mobile_phone="5551112222", current_location=None)
        steps = [[_resume_file_field()], [_city_field()]]
        page, result = self._apply(steps, candidate=candidate, saved_answers=[])
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertFalse(page.submitted)

    def test_never_clicks_submit_and_stops_before_submit(self):
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(),
                                   saved_answers=[self._saved_work_auth()])
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)
        self.assertIn("stopped_before_submit", self._log_steps(result))

    def test_always_returns_a_recorded_status_never_silent(self):
        # Every apply attempt must return an AutomationResult with a status so the
        # batch records it before moving to the next job (no silent close/jump).
        for saved in ([self._saved_work_auth()], []):
            steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
            _, result = self._apply(steps, candidate=make_candidate(), saved_answers=saved)
            self.assertIsNotNone(result)
            self.assertIsNotNone(result.status)
            self.assertIn(result.status, {ApplicationStatus.DRY_RUN_READY, ApplicationStatus.NEEDS_REVIEW,
                                          ApplicationStatus.UNSUPPORTED_FORM, ApplicationStatus.FAILED})

    # --- Browser lifecycle / step progression (the reported regression) ---

    def test_step1_does_not_end_job_immediately(self):
        # Step 1 must navigate to Step 2 (Work Auth + City), not end the job.
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(),
                                   saved_answers=[self._saved_work_auth()])
        self.assertIn(2, self._wizard_steps_opened(result), "runner never opened Step 2")
        self.assertGreaterEqual(page.next_clicks, 1)
        self.assertEqual(page.uploaded_files, [])
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)

    def test_next_clicked_on_step1_without_any_upload(self):
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(),
                                   saved_answers=[self._saved_work_auth()])
        self.assertEqual(page.uploaded_files, [])
        self.assertGreaterEqual(page.next_clicks, 1)
        self.assertIn("next_clicked", self._log_steps(result))

    def test_step2_fields_are_processed(self):
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(),
                                   saved_answers=[self._saved_work_auth("US Citizen")])
        # Work Authorization (from saved answer) and City were both handled on Step 2.
        self.assertTrue(any(e.get("selected_answer") == "US Citizen" for e in result.screening_answers))
        self.assertEqual(page.city_committed_value, "New York, NY, USA")  # committed via autocomplete

    def test_step1_with_no_next_is_needs_review_not_ready(self):
        # Step 1 with no working Next -> next_not_working -> needs_review, never
        # dry_run_ready, and nothing is ever uploaded.
        page = ResumeOnlyNoNextPage()
        page, result = self._apply_page(page, candidate=make_candidate(),
                                        saved_answers=[self._saved_work_auth()])
        self.assertEqual(page.uploaded_files, [], "Step 1 must not upload anything")
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertIn("next_not_working", self._log_steps(result))
        self.assertFalse(page.submitted)

    def test_no_submit_click_in_dry_run(self):
        steps = [[_resume_file_field()], [_work_auth_field(), _city_field()]]
        page, result = self._apply(steps, candidate=make_candidate(),
                                   saved_answers=[self._saved_work_auth()])
        self.assertFalse(page.submitted)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)


class PreUploadedFileInputLocator(FileInputLocator):
    """Dice pre-filled the resume from the candidate's profile: el.files already
    reports a file BEFORE any set_input_files call."""

    def evaluate(self, script, *a, **k):
        return 1


class PreUploadedResumeWizardPage(ScriptedWizardPage):
    """Step 1 already shows the uploaded resume (pre-filled file input); the
    automation must NOT upload again -- just skip the optional cover letter and
    click Next to reach Step 2."""

    def locator(self, selector):
        sel = selector.lower()
        if "type='file'" in sel or 'type="file"' in sel:
            return PreUploadedFileInputLocator(self, True)
        return super().locator(selector)


class PreUploadedResumeNoNextPage:
    """The live report from the user's video: Step 1 'Resume & Cover Letter' with
    the resume ALREADY uploaded (no empty resume input rendered, only the optional
    cover-letter input) and no matchable Next control. Step 1 is NAVIGATION, so
    this must be classified next_not_found -> unsupported_form, NEVER a false
    dry_run_ready that closes the browser and jumps to the next job."""

    WIZARD_URL = "https://www.dice.com/job-applications/pre/wizard"

    def __init__(self):
        self.url = self.WIZARD_URL
        self.uploaded_files: list = []
        self.uploaded_count = 0
        self.confirm_uploads = True
        self.submitted = False
        self.next_clicks = 0

    def goto(self, *a, **k):
        return None

    def wait_for_url(self, *a, **k):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_selector(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        return "<html><!-- resume pre-uploaded, no next --></html>"

    def locator(self, selector):
        sel = selector.lower()
        if "type='file'" in sel or 'type="file"' in sel:
            return PreUploadedFileInputLocator(self, True)
        return FakeLocator(False)  # no Next, no Submit

    def get_by_role(self, role, name):
        return FakeLocator(role == "button" and bool(name.search("Apply")))

    def evaluate(self, script, *args):
        if args:
            return True
        return [_cover_letter_field()]


class LiveDiceStep1Page(CityAutocompleteFake):
    """The EXACT live Dice Step 1 captured in
    outputs/dice_debug/20260709T154057958975/job_33072/apply_form.html:

      * heading "Resume & Cover Letter"
      * the resume is ALREADY uploaded (its filename is visible on the page);
        there is NO resume file input rendered at all
      * the only file input is the sr-only COVER LETTER one, whose sole label
        signal is aria-describedby="cover letter-description"
      * the Next control is <button type="submit"><span>Next</span></button>
        -- no data-cy/data-testid hook

    This is what closed the browser at Step 1: `button[type='submit']` was read as
    a final Submit control, so the runner reported 'reached_submit' -> dry_run_ready
    without ever clicking Next.
    """

    WIZARD_URL = "https://www.dice.com/job-applications/a65596db/wizard"
    RESUME_NAME = "Rishith_Shetty_Resume (1).pdf"

    def __init__(self):
        self.url = self.WIZARD_URL
        self.step = 1
        self.uploaded_files: list = []
        self.uploaded_count = 0
        self.confirm_uploads = True
        self.submitted = False
        self.next_clicks = 0
        self.filled: list = []
        self.captured_html = 0
        self._init_city()

    def goto(self, *a, **k):
        return None

    def wait_for_url(self, *a, **k):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_selector(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        self.captured_html += 1
        return "<html><button type='submit'><span>Next</span></button></html>"

    def _advance(self):
        self.next_clicks += 1
        self.step = 2

    def locator(self, selector):
        sel = selector.lower()
        if "type='file'" in sel or 'type="file"' in sel:
            # Only the sr-only cover-letter input exists, and it holds no file;
            # the resume is already uploaded and shown by filename instead.
            return FakeLocator(False)
        # The live Next button: type=submit + text "Next". A plain
        # "button[type='submit']" (no text filter) must NOT be treated as Submit.
        if "next" in sel:
            return FakeLocator(self.step == 1, on_click=self._advance)
        city = self._city_locator(selector, self.step == 2)
        if city is not None:
            return city
        return FakeLocator(False)

    def get_by_role(self, role, name):
        if bool(name.search("Apply")):
            return FakeLocator(role == "button")
        return FakeLocator(False)

    def get_by_text(self, text, exact=False):
        # The already-uploaded resume's filename is visible on Step 1.
        return FakeLocator(self.step == 1 and text == self.RESUME_NAME)

    def evaluate(self, script, *args):
        if args:
            self.filled.append(args[0])
            return True
        if "h1, h2, h3" in script:
            heading = "Resume & Cover Letter" if self.step == 1 else "Additional Information"
            return f"Demand Planner/ Data Analyst | {heading}"
        if self.step == 1:
            # aria-describedby is the ONLY label signal on the cover-letter input.
            return [{"index": 0, "label": "cover letter-description", "control": "file",
                     "kind": "file", "required": False, "options": []}]
        return [_work_auth_field(), _city_field()]


class LiveDiceStep1NoNextPage(LiveDiceStep1Page):
    """Same live Step 1, but no Next control matches any selector -> the run must
    save page HTML + screenshot and return unsupported_form / next_not_found."""

    def locator(self, selector):
        return FakeLocator(False)


class DiceLiveStep1DomTests(unittest.TestCase):
    """Regression tests pinned to the captured live Dice Step 1 DOM."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.job = make_job()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._tmp.write(b"%PDF-1.4 fake resume")
        self._tmp.close()
        self.resume = make_resume(stored_path=self._tmp.name,
                                  filename=LiveDiceStep1Page.RESUME_NAME)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _saved_work_auth(self, answer="US Citizen"):
        engine = DicePortalAdapter(self.settings).screening
        return {
            "normalized_question": engine.normalize_question("Work Authorization"),
            "answer": answer, "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "work_authorization", "evidence": {},
        }

    def _apply_page(self, page):
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._resume_confirm_max_polls = 3
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        result = adapter.apply_to_job(
            self.job, self.resume, make_candidate(),
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": [self._saved_work_auth()]},
        )
        return adapter, result

    def _log_steps(self, result):
        return [s.get("step") for s in result.log]

    def test_step1_submit_type_next_button_is_clicked_not_treated_as_submit(self):
        page = LiveDiceStep1Page()
        adapter, result = self._apply_page(page)
        self.assertGreaterEqual(page.next_clicks, 1, "the type=submit 'Next' button must be CLICKED")
        self.assertNotEqual(adapter._wizard_stop_code, "reached_submit",
                            "Step 1's type=submit Next button must not read as the review step")
        self.assertEqual(page.uploaded_files, [], "Step 1 must not upload anything")
        self.assertIn("step1_completed", self._log_steps(result))
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_step1_reaches_step2_additional_information(self):
        page = LiveDiceStep1Page()
        _, result = self._apply_page(page)
        self.assertEqual(page.step, 2, "browser must not close at Step 1")
        names = [s.get("step_name") for s in result.log if s.get("step") == "wizard_step_detected"]
        self.assertEqual(names[0], "resume_cover_letter")
        self.assertIn("additional_information", names)

    def test_step1_is_navigation_only_and_touches_no_file_input(self):
        page = LiveDiceStep1Page()
        _, result = self._apply_page(page)
        # The sr-only cover-letter input is never inspected, planned or filled.
        steps = self._log_steps(result)
        self.assertEqual(page.uploaded_files, [])
        for forbidden in ("resume_upload_found", "resume_uploaded", "resume_already_uploaded",
                          "cover_letter_skipped", "resume_upload_failed"):
            self.assertNotIn(forbidden, steps, f"Step 1 must not perform {forbidden}")
        snapshot = next(s for s in result.log if s.get("step") == "wizard_step_snapshot")
        self.assertTrue(snapshot["navigation_only"])
        self.assertEqual(snapshot["fields_detected"], [], "Step 1 fields must be discarded")

    def test_plain_submit_type_button_alone_is_not_a_submit_control(self):
        adapter = DicePortalAdapter(self.settings)
        page = LiveDiceStep1Page()
        self.assertFalse(adapter._has_submit(page),
                         "button[type='submit'] must not be treated as the final Submit control")

    def test_next_not_working_saves_html_and_returns_needs_review(self):
        page = LiveDiceStep1NoNextPage()
        adapter, result = self._apply_page(page)
        self.assertEqual(adapter._wizard_stop_code, "next_not_working")
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertIn("next_not_working", self._log_steps(result))
        self.assertGreaterEqual(page.captured_html, 1, "page HTML must be saved for selector pinning")
        self.assertFalse(page.submitted)


class ObstructedNextLocator(FakeLocator):
    """A Next button that reports, via elementFromPoint, that the consent banner is
    covering its click point while the banner is visible."""

    BLOCKER = {"blocked": True, "tag": "div", "id": "cmpwrapper",
               "cls": "cmpbox", "text": "Consent to Cookies & Data processing Allow all Settings"}

    def __init__(self, page, matched: bool, on_click=None):
        super().__init__(matched, on_click=on_click)
        self._page = page

    def evaluate(self, script, *a, **k):
        return dict(self.BLOCKER) if self._page.banner_visible else None

    def click(self, timeout=None):
        if self._page.banner_visible and self._page.banner_blocks_click:
            raise RuntimeError("element click intercepted by #cmpwrapper")
        super().click(timeout=timeout)


class CookieBannerWizardPage(LiveDiceStep1Page):
    """Live Dice Step 1 with the consentmanager banner from the user's screenshot:
    'Consent to Cookies & Data processing' + 'Allow all' / 'Settings', anchored at
    the bottom over the Next button.

    `banner_at_open`  -- banner already present when the page opens.
    `banner_late`     -- banner injected client-side AFTER the up-front cookie check
                         already ran, so only the Next click can detect and recover
                         from the interception.
    `banner_sticky`   -- 'Allow all' does not actually dismiss it (worst case).
    """

    CMP_CONTAINERS = ("#cmpwrapper", "#cmpbox", ".cmpboxbtn")
    CMP_ACCEPTORS = ("#cmpwelcomebtnyes", "#cmpbox a.cmpboxbtnyes", ".cmpboxbtnyes")

    def __init__(self, banner_at_open=True, banner_late=False, banner_sticky=False):
        super().__init__()
        self.banner_visible = banner_at_open
        self.banner_late = banner_late
        self.banner_sticky = banner_sticky
        self.banner_blocks_click = True
        self.cookie_checked = False
        self.allow_all_clicks = 0
        self.captured_screenshots = 0

    def screenshot(self, *a, **k):
        self.captured_screenshots += 1
        return None

    def _accept_cookies(self):
        self.allow_all_clicks += 1
        if not self.banner_sticky:
            self.banner_visible = False

    def locator(self, selector):
        sel = selector.lower()
        if selector in self.CMP_CONTAINERS:
            self.cookie_checked = True     # the adapter looked for the banner
            return FakeLocator(self.banner_visible)
        if selector in self.CMP_ACCEPTORS or "allow all" in sel:
            return FakeLocator(self.banner_visible, on_click=self._accept_cookies)
        if "type='file'" in sel or 'type="file"' in sel:
            return FakeLocator(False)
        if "next" in sel:
            # The CMP injects its banner only once the page has settled -- i.e. after
            # the adapter's up-front cookie check already came back clean.
            if self.banner_late and self.cookie_checked:
                self.banner_visible = True
            return ObstructedNextLocator(self, self.step == 1, on_click=self._advance)
        city = self._city_locator(selector, self.step == 2)
        if city is not None:
            return city
        return FakeLocator(False)


class DiceCookieBannerTests(unittest.TestCase):
    """Dice-only consent-banner handling: the banner is accepted/dismissed before
    automation continues, and a Next click it intercepts is detected, evidenced,
    recovered from, and retried. Never submits."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.job = make_job()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._tmp.write(b"%PDF-1.4 fake resume")
        self._tmp.close()
        self.resume = make_resume(stored_path=self._tmp.name,
                                  filename=LiveDiceStep1Page.RESUME_NAME)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _saved_work_auth(self):
        engine = DicePortalAdapter(self.settings).screening
        return {
            "normalized_question": engine.normalize_question("Work Authorization"),
            "answer": "US Citizen", "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "work_authorization", "evidence": {},
        }

    def _apply_page(self, page):
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._resume_confirm_max_polls = 3
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        adapter._cookie_banner_max_polls = 3
        result = adapter.apply_to_job(
            self.job, self.resume, make_candidate(),
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": [self._saved_work_auth()]},
        )
        return adapter, result

    def _log_steps(self, result):
        return [s.get("step") for s in result.log]

    def _entries(self, result, step):
        return [s for s in result.log if s.get("step") == step]

    def test_banner_is_accepted_and_gone_before_automation_continues(self):
        page = CookieBannerWizardPage(banner_at_open=True)
        _, result = self._apply_page(page)
        steps = self._log_steps(result)
        self.assertIn("cookie_banner_detected", steps)
        self.assertIn("cookie_banner_dismissed", steps)
        self.assertGreaterEqual(page.allow_all_clicks, 1, "'Allow all' should have been clicked")
        self.assertFalse(page.banner_visible, "automation continued with the banner still up")
        dismissed = self._entries(result, "cookie_banner_dismissed")[0]
        self.assertTrue(dismissed["cookie_banner_dismissed"])
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_no_banner_means_no_cookie_logs_and_next_still_clicked(self):
        page = CookieBannerWizardPage(banner_at_open=False)
        _, result = self._apply_page(page)
        steps = self._log_steps(result)
        self.assertNotIn("cookie_banner_detected", steps)
        self.assertIn("next_click_attempt", steps)
        self.assertIn("next_click_success", steps)
        self.assertGreaterEqual(page.next_clicks, 1)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)

    def test_late_banner_intercepts_next_then_is_cleared_and_retried(self):
        # The banner appears only after the up-front handler ran clean: _click_next
        # must detect the interception, log the offending element, dismiss it, and
        # retry successfully.
        page = CookieBannerWizardPage(banner_at_open=False, banner_late=True)
        _, result = self._apply_page(page)
        steps = self._log_steps(result)
        self.assertIn("click_intercepted_by", steps)
        intercepted = self._entries(result, "click_intercepted_by")[0]
        self.assertEqual(intercepted["element"]["id"], "cmpwrapper")
        self.assertIn("Allow all", intercepted["element"]["text"])
        self.assertGreaterEqual(page.allow_all_clicks, 1, "banner must be dismissed before retry")
        self.assertIn("next_click_success", steps)
        self.assertGreaterEqual(page.next_clicks, 1, "Next must be clicked after the retry")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_intercepted_click_captures_screenshot_and_html(self):
        page = CookieBannerWizardPage(banner_at_open=False, banner_late=True)
        _, result = self._apply_page(page)
        intercepted = self._entries(result, "click_intercepted_by")[0]
        self.assertEqual(intercepted["evidence"], "next_click_intercepted_1")
        self.assertGreaterEqual(page.captured_html, 1, "page HTML must be saved on interception")
        self.assertGreaterEqual(page.captured_screenshots, 1, "screenshot must be saved on interception")

    def test_unclearable_banner_gives_up_with_next_not_working_never_submits(self):
        page = CookieBannerWizardPage(banner_at_open=False, banner_late=True, banner_sticky=True)
        adapter, result = self._apply_page(page)
        # Step 1 exhausts its Next retries -> next_not_working -> needs_review.
        self.assertEqual(adapter._wizard_stop_code, "next_not_working")
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertGreaterEqual(len(self._entries(result, "click_intercepted_by")),
                                adapter._next_click_max_attempts)
        self.assertNotIn("next_click_success", self._log_steps(result))
        self.assertEqual(page.next_clicks, 0)
        self.assertFalse(page.submitted)


class CityInputLocator(FakeLocator):
    """The 'Current city of residence' autocomplete input. Typing into it opens a
    suggestion listbox on the page; the value only commits once a suggestion is
    clicked (exactly like the live Dice control)."""

    def __init__(self, page):
        super().__init__(True)
        self._page = page

    def fill(self, value):
        self._page.city_text = value
        self._page.city_suggestions = self._page._suggestions_for(value)
        self._page.city_suggestions_open = bool(self._page.city_suggestions)

    def type(self, value, delay=None):
        self.fill(value)

    def input_value(self):
        return self._page.city_committed_value

    def click(self, timeout=None):
        return None


class CitySuggestionLocator:
    """The suggestion listbox: `.count()` reflects how many options are showing,
    `.first` is the first option, and clicking it commits its text to the input."""

    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    def count(self):
        return len(self._page.city_suggestions) if self._page.city_suggestions_open else 0

    def is_visible(self):
        return self.count() > 0

    def is_enabled(self):
        return True

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def inner_text(self, timeout=None):
        return self._page.city_suggestions[0]

    def click(self, timeout=None):
        self._page.city_suggestion_clicks += 1
        if self._page.commit_on_suggestion_click:
            self._page.city_committed_value = self._page.city_suggestions[0]
        self._page.city_suggestions_open = False


class LiveDiceWizardPage(CityAutocompleteFake):
    """The full live Dice wizard: Step 1 Resume & Cover Letter (resume already
    uploaded) -> Step 2 Additional Information (Work Authorization + Current city
    autocomplete) -> Step 3 Review (Submit present, never clicked)."""

    WIZARD_URL = "https://www.dice.com/job-applications/a65596db/wizard"
    RESUME_NAME = "Jhanvi_Patel_Resume.pdf"
    HEADINGS = {1: "Resume & Cover Letter", 2: "Additional Information", 3: "Review your application"}

    def __init__(self, work_auth_options=None, missing_required=None,
                 city_suggestions=None, commit_on_suggestion_click=True):
        self.url = self.WIZARD_URL
        self.step = 1
        self.uploaded_files: list = []
        self.uploaded_count = 0
        self.confirm_uploads = True
        self.submitted = False
        self.next_clicks = 0
        self.filled: list = []
        self.captured_html = 0
        self.work_auth_options = work_auth_options or ["US Citizen", "Green Card", "H1B", "Not authorized"]
        self.missing_required = missing_required or []
        self._init_city(city_suggestions, commit_on_suggestion_click)

    def goto(self, *a, **k):
        return None

    def wait_for_url(self, *a, **k):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_selector(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def content(self):
        self.captured_html += 1
        return "<html><!-- dice wizard --></html>"

    def _advance(self):
        self.next_clicks += 1
        self.step = min(self.step + 1, 3)

    def locator(self, selector):
        sel = selector.lower()
        if "type='file'" in sel or 'type="file"' in sel:
            return FakeLocator(False)          # resume pre-uploaded; only sr-only cover letter
        if "next" in sel:                       # checked before "submit": Next is type=submit
            return FakeLocator(self.step < 3, on_click=self._advance)
        if "submit" in sel:
            return FakeLocator(self.step == 3)  # present only on the Review step
        city = self._city_locator(selector, self.step == 2)
        if city is not None:
            return city
        return FakeLocator(False)

    def get_by_role(self, role, name):
        return FakeLocator(role == "button" and bool(name.search("Apply")))

    def get_by_text(self, text, exact=False):
        return FakeLocator(self.step == 1 and text == self.RESUME_NAME)

    def evaluate(self, script, *args):
        if args:
            self.filled.append(args[0])
            return True
        # `seenGroups` is unique to the field-collection script; check it FIRST,
        # since that script also mentions el.required.
        if "seenGroups" not in script:
            if "h1, h2, h3" in script:
                return self.HEADINGS[self.step]
            if "progressbar" in script:
                return f"Step {self.step} of 3"
            if "innerHTML" in script:
                return f"1000:5:/job-applications/a65596db/wizard#{self.step}"
            if "el.required &&" in script:
                return list(self.missing_required) if self.step == 3 else []
        if self.step == 1:
            return [{"index": 0, "label": "cover letter-description", "control": "file",
                     "kind": "file", "required": False, "options": []}]
        if self.step == 2:
            return [
                {"index": 0, "label": "Work Authorization", "control": "select", "kind": "select",
                 "required": True, "options": list(self.work_auth_options)},
                {"index": 1, "label": "Current city of residence", "control": "text", "kind": "text",
                 "required": True, "options": []},
            ]
        return []


class DomDumpingWizardPage(LiveDiceWizardPage):
    """A wizard whose city autocomplete never opens, but whose DOM-dump probe answers
    like a real page: a combobox input wired via aria-controls to a listbox, plus
    elements mentioning the city and the structural keywords."""

    CITY_INPUT = {
        "tag": "input", "id": "city-input", "cls": "dice-combobox",
        "role": "combobox", "text": "", "visible": True,
        "attrs": {"role": "combobox", "aria-controls": "city-listbox",
                  "aria-autocomplete": "list", "aria-expanded": "false",
                  "name": "currentCity", "aria-label": "Current city of residence"},
        "outer_html": "<input id='city-input' role='combobox' aria-controls='city-listbox'>",
    }
    REGION_HTML = ("<div class='field'><label>Current city of residence</label>"
                   "<input id='city-input' role='combobox' aria-controls='city-listbox'>"
                   "<ul id='city-listbox' role='listbox' hidden></ul></div>")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_screenshots = 0

    def screenshot(self, *a, **k):
        self.captured_screenshots += 1
        return None

    def evaluate(self, script, *args):
        if not args and "input_selector_matched" in script:
            return {
                "url": self.url,
                "input_selector_matched": "input[name*='city' i]",
                "city_input": dict(self.CITY_INPUT),
                "aria": {"role": "combobox", "aria-controls": "city-listbox",
                         "aria-autocomplete": "list",
                         "aria-controls_target": {"tag": "ul", "id": "city-listbox",
                                                  "role": "listbox", "text": "", "visible": False}},
                "siblings_of_input": [{"tag": "ul", "id": "city-listbox", "role": "listbox",
                                       "text": "", "visible": False}],
                "region_html_length": len(self.REGION_HTML),
                "region_html": self.REGION_HTML,
                "matches": [
                    {"tag": "ul", "id": "city-listbox", "role": "listbox", "text": "",
                     "matched_token": "listbox", "visible": False},
                    {"tag": "li", "id": "", "role": "option", "text": "Pittsburgh, PA, USA",
                     "matched_token": "Pittsburgh", "visible": False},
                ],
            }
        return super().evaluate(script, *args)


class DiceStep2Tests(unittest.TestCase):
    """Step 2 (Additional Information) is populated from the Candidate Profile and
    the ALREADY-PARSED resume -- never by re-parsing -- then advances to Review and
    stops before Submit."""

    RESUME_TEXT = ("Jhanvi Patel\nHealthcare Data Analyst\nPittsburgh, PA\n"
                   "+1 937-241-1446\njhanviatwork2002@gmail.com\n")

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.job = make_job()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._tmp.write(b"%PDF-1.4 fake resume")
        self._tmp.close()
        self.resume = make_resume(stored_path=self._tmp.name,
                                  filename=LiveDiceWizardPage.RESUME_NAME,
                                  raw_text=self.RESUME_TEXT)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _saved_work_auth(self, answer):
        engine = DicePortalAdapter(self.settings).screening
        return {
            "normalized_question": engine.normalize_question("Work Authorization"),
            "answer": answer, "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "work_authorization", "evidence": {},
        }

    def _apply(self, page, candidate=None, saved_answers=None):
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._resume_confirm_max_polls = 3
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        adapter._cookie_banner_max_polls = 2
        result = adapter.apply_to_job(
            self.job, self.resume,
            candidate if candidate is not None else self._profile_without_city(),
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": saved_answers or []},
        )
        return adapter, result

    def _profile_without_city(self):
        return CandidateProfile(first_name="Jhanvi", last_name="Patel",
                                email="jhanviatwork2002@gmail.com",
                                mobile_phone="9372411446", current_location=None)

    def _steps(self, result):
        return [s.get("step") for s in result.log]

    def _entry(self, result, step):
        return next((s for s in result.log if s.get("step") == step), None)

    def _filled_labels(self, page):
        return [e.get("label") for plan in page.filled for e in plan]

    # --- the exact log sequence the workflow requires ---

    def test_full_step2_to_review_log_sequence(self):
        page = LiveDiceWizardPage()
        _, result = self._apply(page)
        steps = self._steps(result)
        for expected in ("step2_started", "work_authorization_selected", "city_detected",
                         "city_filled", "step2_completed", "review_page_reached", "ready_for_submit"):
            self.assertIn(expected, steps, f"missing log: {expected}")
        # ...and in order.
        order = [steps.index(name) for name in
                 ("step2_started", "work_authorization_selected", "city_filled",
                  "step2_completed", "review_page_reached", "ready_for_submit")]
        self.assertEqual(order, sorted(order), "step 2 logs are out of order")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_review_reached_and_stops_before_submit(self):
        page = LiveDiceWizardPage()
        _, result = self._apply(page)
        self.assertEqual(page.step, 3, "must advance through Step 2 to the Review step")
        self.assertEqual(page.next_clicks, 2, "Next clicked on Step 1 and Step 2")
        self.assertIn("stopped_before_submit", self._steps(result))
        self.assertFalse(page.submitted)
        self.assertEqual(self._entry(result, "ready_for_submit")["submitted"], False)

    # --- work authorization ---

    def test_work_authorization_defaults_to_configured_value_and_matches_option(self):
        page = LiveDiceWizardPage()
        _, result = self._apply(page)
        selected = self._entry(result, "work_authorization_selected")
        self.assertEqual(selected["value"], "US Citizen")
        self.assertEqual(selected["source"], "configured_default")
        self.assertIn("Work Authorization", self._filled_labels(page))
        self.assertTrue(any(e.get("selected_answer") == "US Citizen" for e in result.screening_answers))

    def test_saved_answer_beats_configured_default(self):
        page = LiveDiceWizardPage()
        _, result = self._apply(page, saved_answers=[self._saved_work_auth("Green Card")])
        selected = self._entry(result, "work_authorization_selected")
        self.assertEqual(selected["value"], "Green Card")
        self.assertEqual(selected["source"], "screening_engine")

    def test_configured_value_matched_case_insensitively_to_real_option(self):
        page = LiveDiceWizardPage(work_auth_options=["US CITIZEN", "H1B"])
        _, result = self._apply(page)
        self.assertEqual(self._entry(result, "work_authorization_selected")["value"], "US CITIZEN")

    def test_unmatchable_work_authorization_is_needs_review_not_a_guess(self):
        # The configured "US Citizen" matches none of these -> never pick arbitrarily.
        page = LiveDiceWizardPage(work_auth_options=["Green Card", "H1B", "OPT"])
        _, result = self._apply(page)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertNotIn("Work Authorization", self._filled_labels(page))
        self.assertFalse(page.submitted)

    # --- city, from the already-parsed resume ---

    def test_city_from_analyzed_resume_is_clean(self):
        page = LiveDiceWizardPage()
        _, result = self._apply(page)
        detected = self._entry(result, "city_detected")
        self.assertEqual(detected["city"], "Pittsburgh, PA")   # not "Analyst\nPittsburgh, PA"
        self.assertEqual(detected["source"], "resume")
        self.assertEqual(self._entry(result, "city_typed")["city"], "Pittsburgh, PA")
        self.assertEqual(page.city_committed_value, "Pittsburgh, PA, USA")

    def test_city_prefers_candidate_profile_over_resume(self):
        profile = CandidateProfile(first_name="Jhanvi", last_name="Patel",
                                   email="j@example.com", mobile_phone="9372411446",
                                   current_location="Austin, TX")
        page = LiveDiceWizardPage(city_suggestions=["Austin, TX, USA"])
        _, result = self._apply(page, candidate=profile)
        detected = self._entry(result, "city_detected")
        self.assertEqual(detected["city"], "Austin, TX")
        self.assertEqual(detected["source"], "candidate_profile")
        self.assertEqual(page.city_committed_value, "Austin, TX, USA")

    def test_resume_is_not_reparsed(self):
        # The adapter must read already-parsed ResumeProfile data only.
        import app.services.resume_parser as resume_parser

        calls = []
        original = getattr(resume_parser, "parse_resume", None)
        if original is not None:
            resume_parser.parse_resume = lambda *a, **k: calls.append(a) or original(*a, **k)
        try:
            self._apply(LiveDiceWizardPage())
        finally:
            if original is not None:
                resume_parser.parse_resume = original
        self.assertEqual(calls, [], "the resume must never be parsed again during apply")

    def test_missing_city_everywhere_is_needs_review(self):
        self.resume = make_resume(stored_path=self._tmp.name,
                                  filename=LiveDiceWizardPage.RESUME_NAME,
                                  raw_text="Healthcare Data Analyst with SQL and Python.")
        page = LiveDiceWizardPage()
        _, result = self._apply(page)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertFalse(page.submitted)

    # --- city autocomplete ---

    def test_pittsburgh_typed_then_first_suggestion_selected(self):
        page = LiveDiceWizardPage(city_suggestions=["Pittsburgh, PA, USA", "Pittsburgh, KS, USA"])
        _, result = self._apply(page)
        self.assertEqual(page.city_text, "Pittsburgh, PA", "the resume city must be typed")
        self.assertEqual(page.city_suggestion_clicks, 1, "the FIRST suggestion must be clicked once")
        self.assertEqual(page.city_committed_value, "Pittsburgh, PA, USA")
        steps = self._steps(result)
        for expected in ("city_detected", "city_typed", "city_suggestions_found",
                         "city_first_suggestion_text", "city_suggestion_selected",
                         "city_committed", "step2_next_clicked"):
            self.assertIn(expected, steps, f"missing log: {expected}")
        self.assertEqual(self._entry(result, "city_first_suggestion_text")["text"], "Pittsburgh, PA, USA")
        self.assertEqual(self._entry(result, "city_suggestions_found")["count"], 2)
        self.assertEqual(self._entry(result, "city_committed")["committed_as"], "Pittsburgh, PA, USA")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_city_autocomplete_logs_are_in_order(self):
        page = LiveDiceWizardPage()
        _, result = self._apply(page)
        steps = self._steps(result)
        order = [steps.index(n) for n in ("city_detected", "city_typed", "city_suggestions_found",
                                          "city_suggestion_selected", "city_committed", "step2_next_clicked")]
        self.assertEqual(order, sorted(order), "city autocomplete logs are out of order")

    def test_no_suggestion_is_needs_review_with_city_suggestion_not_found(self):
        page = LiveDiceWizardPage(city_suggestions=[])
        adapter, result = self._apply(page)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(adapter._city_commit_failure, "city_suggestion_not_found")
        self.assertIn("city_suggestion_not_found", self._steps(result))
        self.assertIn("no autocomplete suggestions", result.message.lower())
        self.assertEqual(page.city_suggestion_clicks, 0)
        self.assertFalse(page.submitted)

    def test_missing_suggestions_dump_captures_dom_and_evidence(self):
        # A single failed live run must yield everything needed to pin the real Dice
        # selector: screenshot, page HTML, the DOM around the input, and every element
        # mentioning the city / PA / option / listbox / combobox / autocomplete.
        page = DomDumpingWizardPage(city_suggestions=[])
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        adapter._cookie_banner_max_polls = 2
        adapter._city_suggestion_max_polls = 2
        with tempfile.TemporaryDirectory() as debug_root:
            adapter._debug_dir = lambda: Path(debug_root)
            result = adapter.apply_to_job(
                self.job, self.resume, self._profile_without_city(),
                dry_run=True, capture_evidence=False,
                screening_context={"mode": "safe", "saved_answers": []},
            )
            job_dir = Path(debug_root) / f"job_{self.job.id}"
            dump_path = job_dir / "city_autocomplete_debug.json"
            self.assertTrue(dump_path.exists(), "city_autocomplete_debug.json must be written")
            dump = json.loads(dump_path.read_text(encoding="utf-8"))

            self.assertEqual(dump["typed_city"], "Pittsburgh, PA")
            for token in ("Pittsburgh", "PA", "option", "listbox", "combobox", "autocomplete"):
                self.assertIn(token, dump["tokens_searched"])
            self.assertIsNotNone(dump["city_input"])
            self.assertEqual(dump["aria"]["aria-controls"], "city-listbox")
            self.assertTrue(dump["suggestion_selectors_tried"])
            matched = {m["matched_token"] for m in dump["matches"]}
            self.assertTrue({"Pittsburgh", "listbox"} & matched, f"expected token hits, got {matched}")

            # DOM around the input is saved as its own openable file.
            region = job_dir / "city_input_region.html"
            self.assertTrue(region.exists())
            self.assertIn("city-listbox", region.read_text(encoding="utf-8"))
            # Screenshot + full page HTML were captured too.
            self.assertGreaterEqual(page.captured_screenshots, 1)
            self.assertGreaterEqual(page.captured_html, 1)

        # ...and the run stops rather than continuing.
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(adapter._city_commit_failure, "city_suggestion_not_found")
        self.assertEqual(page.next_clicks, 1, "Step 2's Next must not be clicked")
        self.assertFalse(page.submitted)

    def test_next_is_not_clicked_until_city_is_committed(self):
        # No suggestions -> city never commits -> Step 2's Next must NOT be clicked.
        page = LiveDiceWizardPage(city_suggestions=[])
        _, result = self._apply(page)
        self.assertEqual(page.next_clicks, 1, "only Step 1's Next may have been clicked")
        self.assertEqual(page.step, 2, "must not advance past Step 2")
        self.assertNotIn("step2_next_clicked", self._steps(result))
        self.assertNotIn("step2_completed", self._steps(result))
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)

    def test_suggestion_clicked_but_value_not_committed_is_needs_review(self):
        page = LiveDiceWizardPage(commit_on_suggestion_click=False)
        adapter, result = self._apply(page)
        self.assertEqual(page.city_suggestion_clicks, 1)
        self.assertEqual(adapter._city_commit_failure, "city_not_committed")
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertNotIn("step2_next_clicked", self._steps(result))
        self.assertFalse(page.submitted)

    def test_committed_city_is_not_overwritten_by_the_js_filler(self):
        page = LiveDiceWizardPage()
        _, _ = self._apply(page)
        filled_labels = [e.get("label") for plan in page.filled for e in plan]
        self.assertNotIn("Current city of residence", filled_labels,
                         "the JS filler must never overwrite the committed autocomplete value")
        self.assertIn("Work Authorization", filled_labels)
        self.assertEqual(page.city_committed_value, "Pittsburgh, PA, USA")

    # --- review verification ---

    def test_review_with_incomplete_required_field_is_needs_review(self):
        page = LiveDiceWizardPage(missing_required=["work authorization"])
        adapter, result = self._apply(page)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(adapter._wizard_stop_code, "missing_required_answer")
        self.assertIn("review_incomplete", self._steps(result))
        self.assertNotIn("ready_for_submit", self._steps(result))
        self.assertFalse(page.submitted)

    def test_never_submits_in_dry_run(self):
        for page in (LiveDiceWizardPage(),
                     LiveDiceWizardPage(missing_required=["work authorization"]),
                     LiveDiceWizardPage(work_auth_options=["OPT"])):
            _, result = self._apply(page)
            self.assertFalse(page.submitted)
            self.assertIn(result.status, {ApplicationStatus.DRY_RUN_READY,
                                          ApplicationStatus.NEEDS_REVIEW})


class DiceResumeCityTests(unittest.TestCase):
    """Regression: the City,ST matcher must not span a newline."""

    def setUp(self):
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    def test_city_does_not_absorb_the_previous_line(self):
        resume = make_resume(raw_text="Jhanvi Patel\nHealthcare Data Analyst\nPittsburgh, PA\n")
        self.assertEqual(self.adapter._resume_city(resume), "Pittsburgh, PA")

    def test_two_word_city_still_matches(self):
        resume = make_resume(raw_text="Data Analyst based in New York, NY.")
        self.assertEqual(self.adapter._resume_city(resume), "New York, NY")

    def test_no_city_returns_none(self):
        self.assertIsNone(self.adapter._resume_city(make_resume(raw_text="Data Analyst.")))

    def test_extracts_each_resumes_own_city(self):
        for city in ("Pittsburgh, PA", "New York, NY", "Dallas, TX"):
            with self.subTest(city=city):
                resume = make_resume(raw_text=f"Ada Lovelace\nData Analyst\n{city}\nada@example.com\n")
                self.assertEqual(self.adapter._resume_city(resume), city)

    def test_experience_location_is_preferred_over_raw_text(self):
        resume = make_resume(
            raw_text="Ada Lovelace\nData Analyst\nPittsburgh, PA\n",
            experience=[{"company": "Acme", "location": "Dallas, TX"}],
        )
        self.assertEqual(self.adapter._resume_city(resume), "Dallas, TX")


class DiceCityDynamicTests(unittest.TestCase):
    """The Dice city automation is DYNAMIC: it never hardcodes a city. Each resume
    types and selects its own city, sourced from CandidateProfile.current_location
    first and the already-parsed resume raw_text second."""

    CITIES = ("Pittsburgh, PA", "New York, NY", "Dallas, TX")

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.job = make_job()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._tmp.write(b"%PDF-1.4 fake resume")
        self._tmp.close()

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _resume_for(self, city):
        return make_resume(stored_path=self._tmp.name, filename=LiveDiceWizardPage.RESUME_NAME,
                           raw_text=f"Ada Lovelace\nSenior Data Analyst\n{city}\nada@example.com\n")

    def _profile(self, city=None):
        return CandidateProfile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                                mobile_phone="5551112222", current_location=city)

    def _apply(self, resume, profile, page):
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        adapter._cookie_banner_max_polls = 2
        adapter._city_suggestion_max_polls = 3
        result = adapter.apply_to_job(
            self.job, resume, profile, dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": []},
        )
        return result

    def _entry(self, result, step):
        return next((s for s in result.log if s.get("step") == step), None)

    def test_each_resume_types_and_selects_its_own_city_from_raw_text(self):
        for city in self.CITIES:
            with self.subTest(city=city):
                # The fake's suggestions are derived from what is typed, so a
                # hardcoded city could not possibly produce a matching commit.
                page = LiveDiceWizardPage()
                result = self._apply(self._resume_for(city), self._profile(None), page)

                self.assertEqual(self._entry(result, "city_detected")["city"], city)
                self.assertEqual(self._entry(result, "city_detected")["source"], "resume")
                self.assertEqual(page.city_text, city, "the resume's own city must be typed")
                self.assertEqual(self._entry(result, "city_typed")["city"], city)
                self.assertEqual(self._entry(result, "city_first_suggestion_text")["text"], f"{city}, USA")
                self.assertEqual(self._entry(result, "city_suggestion_selected")["text"], f"{city}, USA")
                self.assertEqual(page.city_committed_value, f"{city}, USA")
                self.assertEqual(page.city_suggestion_clicks, 1)
                self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
                self.assertFalse(page.submitted)

    def test_each_profile_city_wins_over_the_resume_city(self):
        for city in self.CITIES:
            with self.subTest(city=city):
                page = LiveDiceWizardPage()
                # Resume says Pittsburgh; the profile must win every time.
                result = self._apply(self._resume_for("Pittsburgh, PA"), self._profile(city), page)

                detected = self._entry(result, "city_detected")
                self.assertEqual(detected["city"], city)
                self.assertEqual(detected["source"], "candidate_profile")
                self.assertEqual(page.city_text, city)
                self.assertEqual(page.city_committed_value, f"{city}, USA")
                self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)

    def test_no_city_is_ever_hardcoded_in_the_adapter_source(self):
        import inspect

        import app.services.portals.dice_portal as dice_module

        source = inspect.getsource(dice_module)
        # Strip comments: the module documents the live bug using a real city name.
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        for city in ("Pittsburgh", "New York", "Dallas", "Austin"):
            self.assertNotIn(city, code, f"{city!r} must not be hardcoded in dice_portal.py")

    def test_three_resumes_produce_three_distinct_committed_cities(self):
        committed = []
        for city in self.CITIES:
            page = LiveDiceWizardPage()
            self._apply(self._resume_for(city), self._profile(None), page)
            committed.append(page.city_committed_value)
        self.assertEqual(committed, [f"{c}, USA" for c in self.CITIES])
        self.assertEqual(len(set(committed)), 3, "each resume must yield its own city")


class DiceInstrumentationTests(unittest.TestCase):
    """Every way a Dice apply run can end must record WHY, at which source line, and
    with a per-step snapshot. There is no unexplained browser close."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.job = make_job()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._tmp.write(b"%PDF-1.4 fake resume")
        self._tmp.close()
        self.resume = make_resume(stored_path=self._tmp.name,
                                  filename=LiveDiceStep1Page.RESUME_NAME)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _saved_work_auth(self):
        engine = DicePortalAdapter(self.settings).screening
        return {
            "normalized_question": engine.normalize_question("Work Authorization"),
            "answer": "US Citizen", "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "work_authorization", "evidence": {},
        }

    def _apply_page(self, page, saved_answers=None):
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._resume_confirm_max_polls = 3
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        adapter._cookie_banner_max_polls = 3
        result = adapter.apply_to_job(
            self.job, self.resume, make_candidate(),
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe",
                               "saved_answers": [self._saved_work_auth()] if saved_answers is None else saved_answers},
        )
        return adapter, result

    def _events(self, adapter, name):
        return [e for e in adapter._wizard_events if e.get("event") == name]

    def _returns(self, adapter):
        return self._events(adapter, "return_path")

    def test_every_run_records_a_return_path_with_source_line(self):
        for page in (LiveDiceStep1Page(), LiveDiceStep1NoNextPage()):
            adapter, result = self._apply_page(page)
            returns = self._returns(adapter)
            self.assertTrue(returns, f"{type(page).__name__}: no return_path recorded")
            final = [r for r in returns if r.get("where") == "apply_to_job"]
            self.assertTrue(final, "apply_to_job must record why it returned")
            for entry in returns:
                self.assertTrue(entry.get("reason"), "every return must state a reason")
                self.assertRegex(entry.get("at", ""), r"dice_portal\.py:\d+ in \w+",
                                 "every return must record file:line")
            self.assertIsNotNone(result.status)

    def test_step_snapshot_captures_every_requested_field(self):
        page = LiveDiceStep1Page()
        adapter, _ = self._apply_page(page)
        snaps = self._events(adapter, "wizard_step_snapshot")
        self.assertGreaterEqual(len(snaps), 2, "both wizard steps must be snapshotted")
        first = snaps[0]
        for key in ("current_step_name", "current_step_number", "progress_bar_text", "page_heading",
                    "fields_detected", "navigation_only",
                    "next_button_found", "next_button_selector", "page_url", "dom_fingerprint"):
            self.assertIn(key, first, f"snapshot missing {key}")
        self.assertEqual(first["current_step_number"], 1)
        self.assertEqual(first["current_step_name"], "resume_cover_letter")
        self.assertIn("Resume & Cover Letter", first["page_heading"])
        self.assertTrue(first["navigation_only"], "Step 1 is navigation-only")
        self.assertTrue(first["next_button_found"])
        self.assertEqual(snaps[1]["current_step_name"], "additional_information")

    def test_advance_records_url_before_after_and_dom_changed(self):
        page = LiveDiceStep1Page()
        adapter, _ = self._apply_page(page)
        clicked = self._events(adapter, "next_clicked")
        self.assertTrue(clicked, "a successful Next click must be traced")
        for key in ("url_before", "url_after", "dom_changed"):
            self.assertIn(key, clicked[0])

    def test_wizard_stop_records_code_reason_and_line(self):
        page = LiveDiceStep1NoNextPage()
        adapter, result = self._apply_page(page)
        stops = self._events(adapter, "wizard_stop")
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["code"], "next_not_working")
        self.assertTrue(stops[0]["reason"])
        self.assertRegex(stops[0]["stop_at"], r"dice_portal\.py:\d+ in _navigate_step1")
        self.assertEqual(adapter._wizard_stop_at, stops[0]["stop_at"])
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)

    def test_click_next_return_explains_next_not_found(self):
        page = LiveDiceStep1NoNextPage()
        adapter, _ = self._apply_page(page)
        click_returns = [r for r in self._returns(adapter) if r["where"] == "_click_next"]
        self.assertTrue(click_returns)
        self.assertEqual(click_returns[0]["reason_code"], "next_not_found")
        self.assertIn("selectors_tried", click_returns[0])

    def test_browser_context_close_is_preceded_by_a_recorded_reason(self):
        page = LiveDiceStep1Page()
        adapter, _ = self._apply_page(page)
        names = [e["event"] for e in adapter._wizard_events]
        self.assertIn("browser_context_opened", names)
        self.assertIn("browser_context_closing", names)
        # The close is always AFTER apply_to_job has recorded its return reason.
        close_at = names.index("browser_context_closing")
        return_at = next(i for i, e in enumerate(adapter._wizard_events)
                         if e["event"] == "return_path" and e.get("where") == "apply_to_job")
        self.assertLess(return_at, close_at, "browser closed before the reason was recorded")

    def test_plan_dry_run_records_status_on_every_path(self):
        # dry_run_ready path
        adapter, result = self._apply_page(LiveDiceStep1Page())
        plan_returns = [r for r in self._returns(adapter) if r["where"] == "_plan_dry_run"]
        self.assertEqual(plan_returns[0]["status"], "dry_run_ready")
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        # needs_review path (Step 1 never advanced)
        adapter, result = self._apply_page(LiveDiceStep1NoNextPage())
        plan_returns = [r for r in self._returns(adapter) if r["where"] == "_plan_dry_run"]
        self.assertEqual(plan_returns[0]["status"], "needs_review")
        self.assertEqual(plan_returns[0]["stop_code"], "next_not_working")
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)

    def test_exception_return_path_includes_stack_trace(self):
        # An unhandled error inside the browser context (here: new_page) must still
        # classify the job and record the full stack, never close silently.
        class ExplodingContextDice(RealDetectionDice):
            @contextlib.contextmanager
            def _apply_browser_context(self):
                class Ctx:
                    def new_page(self):
                        raise RuntimeError("boom from the wizard")
                yield Ctx()

        adapter = ExplodingContextDice(self.settings, LiveDiceStep1Page())
        result = adapter.apply_to_job(
            self.job, self.resume, make_candidate(),
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": []},
        )
        self.assertEqual(result.status, ApplicationStatus.FAILED)
        exc_returns = [r for r in self._returns(adapter)
                       if r["where"] == "apply_to_job" and r.get("error_type") == "RuntimeError"]
        self.assertTrue(exc_returns, "an unhandled exception must record a return_path")
        self.assertIn("stack", exc_returns[0])
        self.assertIn("boom from the wizard", exc_returns[0]["traceback"])
        self.assertRegex(exc_returns[0]["at"], r"dice_portal\.py:\d+ in apply_to_job")

    def test_detect_apply_form_explains_external_and_none(self):
        class ExternalApplyPage(LiveDiceStep1Page):
            def locator(self, selector):
                if "Apply on company site" in selector:
                    return FakeLocator(True)
                return super().locator(selector)

        adapter, result = self._apply_page(ExternalApplyPage())
        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)
        detect_returns = [r for r in self._returns(adapter) if r["where"] == "_detect_apply_form"]
        self.assertTrue(detect_returns)
        self.assertIn("external", detect_returns[0]["reason"])
        self.assertIn("matched", str(self._events(adapter, "external_apply_detected")))


class DiceStep1NavigationTests(unittest.TestCase):
    """Dice Step 1 (Resume & Cover Letter) is a NAVIGATION step: an already
    uploaded resume is detected and never re-uploaded, the optional cover letter
    is skipped, Next is clicked to reach Step 2, and Step 1 can never end the job
    as dry_run_ready."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.job = make_job()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._tmp.write(b"%PDF-1.4 fake resume")
        self._tmp.close()
        self.resume = make_resume(stored_path=self._tmp.name, filename="resume.pdf")

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _saved_work_auth(self, answer="US Citizen"):
        engine = DicePortalAdapter(self.settings).screening
        return {
            "normalized_question": engine.normalize_question("Work Authorization"),
            "answer": answer, "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "work_authorization", "evidence": {},
        }

    def _apply_page(self, page, candidate=None, saved_answers=None):
        adapter = RealDetectionDice(self.settings, page)
        adapter._apply_panel_max_polls = 6
        adapter._resume_confirm_max_polls = 3
        adapter._next_button_max_polls = 3
        adapter._next_step_max_polls = 3
        result = adapter.apply_to_job(
            self.job, self.resume, candidate,
            dry_run=True, capture_evidence=False,
            screening_context={"mode": "safe", "saved_answers": saved_answers or []},
        )
        return adapter, result

    def _log_steps(self, result):
        return [s.get("step") for s in result.log]

    def test_step1_with_resume_present_clicks_next_without_touching_it(self):
        steps = [[_resume_file_field(), _cover_letter_field()],
                 [_work_auth_field(), _city_field()], []]
        page = PreUploadedResumeWizardPage(steps)
        _, result = self._apply_page(page, candidate=make_candidate(),
                                     saved_answers=[self._saved_work_auth()])
        self.assertEqual(page.uploaded_files, [], "Step 1 must not upload or replace the resume")
        self.assertGreaterEqual(page.next_clicks, 1, "Next must be clicked on Step 1")
        steps_seen = self._log_steps(result)
        self.assertIn("step1_entered", steps_seen)
        self.assertIn("step1_completed", steps_seen)
        self.assertIn("step2_entered", steps_seen)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_step1_with_only_cover_letter_input_still_advances(self):
        # Dice renders NO resume input (resume shown as already uploaded); only the
        # cover-letter input exists. It must be ignored entirely.
        steps = [[_cover_letter_field()], [_work_auth_field(), _city_field()], []]
        page = PreUploadedResumeWizardPage(steps)
        _, result = self._apply_page(page, candidate=make_candidate(),
                                     saved_answers=[self._saved_work_auth()])
        self.assertEqual(page.uploaded_files, [])
        self.assertGreaterEqual(page.next_clicks, 1)
        self.assertIn("step1_completed", self._log_steps(result))
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(page.submitted)

    def test_step1_never_uploads_even_when_no_file_is_present(self):
        # Previously this uploaded the analyzed resume; now Step 1 touches nothing.
        steps = [[_resume_file_field(), _cover_letter_field()],
                 [_work_auth_field(), _city_field()], []]
        page = ScriptedWizardPage(steps)
        _, result = self._apply_page(page, candidate=make_candidate(),
                                     saved_answers=[self._saved_work_auth()])
        self.assertEqual(page.uploaded_files, [], "no upload may ever occur on Step 1")
        self.assertGreaterEqual(page.next_clicks, 1)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)

    def test_step1_without_next_is_needs_review_with_next_not_working(self):
        page = PreUploadedResumeNoNextPage()
        adapter, result = self._apply_page(page, candidate=make_candidate(),
                                           saved_answers=[self._saved_work_auth()])
        self.assertNotEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(adapter._wizard_stop_code, "next_not_working")
        self.assertIn("next_not_working", self._log_steps(result))
        self.assertEqual(page.uploaded_files, [])
        self.assertFalse(page.submitted)

    def test_step1_retries_next_three_times_before_giving_up(self):
        page = PreUploadedResumeNoNextPage()
        adapter, _ = self._apply_page(page, candidate=make_candidate(),
                                      saved_answers=[self._saved_work_auth()])
        attempts = [s for s in adapter._wizard_events if s["event"] == "next_button_found"]
        self.assertEqual(len(attempts), adapter._step1_max_next_attempts)

    def test_step_names_are_detected_and_logged(self):
        steps = [[_resume_file_field(), _cover_letter_field()],
                 [_work_auth_field(), _city_field()], []]
        page = PreUploadedResumeWizardPage(steps)
        _, result = self._apply_page(page, candidate=make_candidate(),
                                     saved_answers=[self._saved_work_auth()])
        names = [s.get("step_name") for s in result.log if s.get("step") == "wizard_step_detected"]
        self.assertEqual(names[:3], ["resume_cover_letter", "additional_information", "review_submit"])
        self.assertIn("next_step_detected", self._log_steps(result))

    def test_step1_always_records_final_status_before_close(self):
        # The browser may only close after a terminal classification is recorded,
        # with the wizard stop reason preserved for the trace.
        cases = [
            PreUploadedResumeWizardPage([[_resume_file_field()],
                                         [_work_auth_field(), _city_field()], []]),
            PreUploadedResumeNoNextPage(),
        ]
        for page in cases:
            adapter, result = self._apply_page(page, candidate=make_candidate(),
                                               saved_answers=[self._saved_work_auth()])
            self.assertIsNotNone(result)
            self.assertIn(result.status, {ApplicationStatus.DRY_RUN_READY, ApplicationStatus.NEEDS_REVIEW,
                                          ApplicationStatus.UNSUPPORTED_FORM, ApplicationStatus.FAILED})
            self.assertIsNotNone(adapter._wizard_stop_code, "wizard must record why it stopped")
            self.assertFalse(page.submitted)


if __name__ == "__main__":
    unittest.main()
