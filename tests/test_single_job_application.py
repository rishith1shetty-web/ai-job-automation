import json
import re
import unittest
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.config import Settings
from app.main import (
    _batch_job_status,
    _create_apply_top_batch,
    _run_apply_top_batch,
    apply_selected_job,
    apply_top_jobs,
    save_extracted_employment_history,
    set_active_portal,
)
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    ApplyTopBatch,
    CandidateProfile,
    CandidateEmploymentHistory,
    JobOpportunity,
    JobSearchBatch,
    ResumeProfile,
)
from app.schemas import ApplyRequest, ApplySelectedRequest
from app.services.linkedin import AutomationResult, LinkedInAutomation
from app.services.portals.dice_portal import DicePortalAdapter


@contextmanager
def _fake_dice_ctx(self):
    """Fake browser context so Dice dry-run apply tests never launch a real browser."""

    class _Pg:
        def goto(self, *a, **k):
            return None

        def screenshot(self, *a, **k):
            return None

        def wait_for_timeout(self, *a, **k):
            return None

    class _Ctx:
        def new_page(self):
            return _Pg()

    yield _Ctx()


class FakeApplicationAutomation:
    processed_job_ids: list[int] = []
    processed_resume_ids: list[int] = []
    processed_resume_paths: list[str] = []

    def __init__(self, _settings) -> None:
        pass

    def apply_to_job(
        self,
        job,
        _resume,
        _candidate_profile,
        dry_run,
        capture_evidence=False,
        screening_context=None,
    ) -> AutomationResult:
        self.__class__.processed_job_ids.append(job.id)
        self.__class__.processed_resume_ids.append(_resume.id)
        self.__class__.processed_resume_paths.append(_resume.stored_path)
        return AutomationResult(
            status=ApplicationStatus.DRY_RUN_READY if dry_run else ApplicationStatus.APPLIED,
            message="Dry run completed." if dry_run else None,
            log=[{"event": "processed", "job_id": job.id}],
            current_step="dry_run_complete" if dry_run else "confirmation_received",
            evidence_path="outputs/live_test/test" if capture_evidence else None,
        )


class FailingSecondJobAutomation(FakeApplicationAutomation):
    def apply_to_job(
        self,
        job,
        _resume,
        _candidate_profile,
        dry_run,
        capture_evidence=False,
        screening_context=None,
    ) -> AutomationResult:
        self.__class__.processed_job_ids.append(job.id)
        if len(self.__class__.processed_job_ids) == 2:
            raise RuntimeError("Synthetic per-job failure")
        return AutomationResult(
            status=ApplicationStatus.DRY_RUN_READY,
            message="Dry run completed.",
            log=[{"event": "processed", "job_id": job.id}],
            current_step="dry_run_complete",
            evidence_path="outputs/live_test/test" if capture_evidence else None,
        )


class RadioFailureSecondJobAutomation(FakeApplicationAutomation):
    processed_job_ids: list[int] = []

    def apply_to_job(
        self,
        job,
        _resume,
        _candidate_profile,
        dry_run,
        capture_evidence=False,
        screening_context=None,
    ) -> AutomationResult:
        self.__class__.processed_job_ids.append(job.id)
        if len(self.__class__.processed_job_ids) == 2:
            raise RuntimeError("Radio option was clicked but LinkedIn did not accept the selection.")
        return AutomationResult(
            status=ApplicationStatus.DRY_RUN_READY,
            message="Dry run completed.",
            log=[{"event": "processed", "job_id": job.id}],
            current_step="dry_run_complete",
            evidence_path="outputs/live_test/test" if capture_evidence else None,
        )


class FakeLocator:
    def __init__(
        self,
        count=0,
        text="",
        enabled=True,
        texts=None,
        href="",
        attributes=None,
    ) -> None:
        self._count = count
        self._text = text
        self._enabled = enabled
        self._texts = texts or ([text] if text else [])
        self._href = href
        self._attributes = attributes or {}
        self.pressed_keys = []
        self.first = self

    def count(self):
        return self._count

    def click(self, **_kwargs):
        return None

    def is_enabled(self):
        return self._enabled

    def inner_text(self, **_kwargs):
        return self._text

    def nth(self, _index):
        return self

    def set_input_files(self, _path):
        return None

    def fill(self, value):
        self._attributes["value"] = value

    def press(self, key, **_kwargs):
        self.pressed_keys.append(key)
        return None

    def select_option(self, label=None, value=None):
        self._attributes["value"] = label or value

    def wait_for(self, state=None, timeout=None):
        return None

    def is_visible(self):
        return self._count > 0

    def locator(self, selector):
        return FakeLocator()

    def get_by_role(self, role, name=None):
        return FakeLocator()

    def evaluate(self, _expression, _arg=None):
        return ""

    def evaluate_all(self, _expression):
        return []

    def all_inner_texts(self):
        return self._texts

    def get_attribute(self, name):
        if name == "href":
            return self._href
        return self._attributes.get(name)


class FakeEvidencePage:
    url = "https://www.linkedin.com/jobs/view/123"

    def goto(self, _url, wait_until=None):
        return None

    def wait_for_load_state(self, _state, timeout=None):
        return None

    def wait_for_timeout(self, _milliseconds):
        return None

    def screenshot(self, path, full_page=True):
        Path(path).write_bytes(b"test-image")

    def get_by_role(self, role, name=None):
        pattern = getattr(name, "pattern", "")
        if role == "button" and "Easy Apply" in pattern:
            return FakeLocator(count=1, text="Easy Apply")
        if role == "button" and "Submit application" in pattern:
            return FakeLocator(count=1, text="Submit application")
        return FakeLocator()

    def get_by_text(self, _text):
        return FakeLocator()

    def locator(self, selector):
        if selector in {
            ".jobs-easy-apply-modal",
            "[role='dialog'][aria-modal='true']",
            "[data-test-modal]",
        }:
            return FakeEasyApplyModal()
        if selector == "input[type='file']":
            return FakeLocator(count=1)
        if selector == "input[required], textarea[required], select[required]":
            return FakeLocator()
        if selector == "button:visible, a[role='button']:visible":
            return FakeLocator(texts=["Easy Apply", "Save"])
        return FakeLocator()

    def content(self):
        return "<html><body>Fake LinkedIn job page</body></html>"

    def title(self):
        return "Fake LinkedIn Job"


class FakeEasyApplyModal(FakeLocator):
    def __init__(self) -> None:
        super().__init__(count=1, text="Easy Apply form")

    def locator(self, selector):
        if selector == "input[type='file']":
            return FakeLocator(count=1)
        if selector.startswith("input[required]"):
            return FakeLocator()
        if selector == "button:visible":
            return FakeLocator(texts=["Submit application"])
        if selector.startswith("input:visible"):
            return FakeLocator()
        return FakeLocator()

    def get_by_role(self, role, name=None):
        pattern = getattr(name, "pattern", "")
        if role == "button" and "Submit application" in pattern:
            return FakeLocator(count=1, text="Submit application")
        return FakeLocator()

    def evaluate(self, expression, _arg=None):
        if expression == "el => el.outerHTML":
            return "<div role='dialog'>Easy Apply form</div>"
        return ""


class EmploymentField(FakeLocator):
    def __init__(
        self,
        label: str,
        tag: str = "input",
        field_type: str = "text",
        required: bool = True,
        value: str = "",
        options: list[str] | None = None,
    ) -> None:
        super().__init__(count=1, text=label, attributes={"value": value})
        self.label = label
        self.tag = tag
        self.field_type = field_type
        self.required = required
        self.options = options or []
        self.clicked = False

    def evaluate(self, expression, _arg=None):
        if "Array.from(el.options)" in expression:
            return self.options
        if "tagName.toLowerCase" in expression:
            value = self._attributes.get("value") or ""
            return {
                "tag": self.tag,
                "type": self.field_type,
                "field_type": "select" if self.tag == "select" else self.field_type,
                "required": self.required,
                "has_value": bool(str(value).strip()) or self.clicked,
            }
        if "el.name" in expression:
            return self.label
        return self.label

    def fill(self, value):
        self._attributes["value"] = value

    def select_option(self, label=None, value=None):
        self._attributes["value"] = label or value

    def click(self, **_kwargs):
        self.clicked = True
        self._attributes["value"] = "checked"

    def check(self, **_kwargs):
        self.click()


class EmploymentFieldsLocator(FakeLocator):
    def __init__(self, fields: list[EmploymentField]) -> None:
        super().__init__(count=len(fields))
        self.fields = fields

    def count(self):
        return len(self.fields)

    def nth(self, index):
        return self.fields[index]

    def evaluate_all(self, _expression):
        return []

    def all_inner_texts(self):
        return []


class EmploymentHistoryModal(FakeEasyApplyModal):
    def __init__(self, fields: list[EmploymentField]) -> None:
        super().__init__()
        self.fields = fields

    def locator(self, selector):
        if selector in {
            "input:visible, textarea:visible, select:visible",
            "input[required]:visible, textarea[required]:visible, select[required]:visible",
        }:
            fields = self.fields
            if selector.startswith("input[required]"):
                fields = [field for field in self.fields if field.required]
            return EmploymentFieldsLocator(fields)
        if "error" in selector or "alert" in selector or "artdeco-inline-feedback" in selector:
            return FakeLocator(count=0, texts=[])
        if selector == "button:visible":
            return FakeLocator(count=1, text="Next", texts=["Next"])
        return super().locator(selector)


class EmploymentEditModal(EmploymentHistoryModal):
    def locator(self, selector):
        if selector == "button:visible":
            return FakeLocator(count=3, text="Save", texts=["Save", "Back", "Next"])
        return super().locator(selector)

    def get_by_role(self, role, name=None):
        pattern = getattr(name, "pattern", "")
        if role == "button" and "Save" in pattern:
            return FakeLocator(count=1, text="Save")
        if role == "button" and "Next" in pattern:
            return FakeLocator(count=1, text="Next")
        return FakeLocator()

    def inner_text(self, **_kwargs):
        return "Your title\nCompany\nI currently work here\nMonth of From\nMonth of To\nSave\nNext"


class ContinueEasyApplyModal(FakeEasyApplyModal):
    def locator(self, selector):
        if selector == "button:visible":
            return FakeLocator(count=1, texts=["Continue"])
        return super().locator(selector)

    def get_by_role(self, role, name=None):
        pattern = getattr(name, "pattern", "")
        if role == "button" and "Continue" in pattern:
            return FakeLocator(count=1, text="Continue")
        return FakeLocator()


class VisibleOnlyNextModal(FakeEasyApplyModal):
    def locator(self, selector):
        if selector == "button:visible":
            return FakeLocator(count=1, text="Next", texts=["Next"])
        return super().locator(selector)

    def get_by_role(self, role, name=None):
        return FakeLocator()


class UploadResumeRequiredModal(FakeEasyApplyModal):
    def locator(self, selector):
        if selector == "input[type='file']":
            return FakeLocator(count=1)
        return super().locator(selector)

    def inner_text(self, **_kwargs):
        return "Resume\nBe sure to include an updated resume *\nUpload resume\nDOC, DOCX, PDF (2 MB)\nA resume is required"


class EmptyEasyApplyModal(FakeEasyApplyModal):
    def get_by_role(self, role, name=None):
        return FakeLocator()


class ValidationErrorModal(FakeEasyApplyModal):
    def locator(self, selector):
        if "error" in selector or "alert" in selector or "artdeco-inline-feedback" in selector:
            return FakeLocator(
                count=1,
                texts=["Please upload an acceptable document format (DOC, DOCX, PDF). Change file"],
            )
        if selector == "button:visible":
            return FakeLocator(count=1, texts=["Back", "Next"])
        return super().locator(selector)

    def get_by_role(self, role, name=None):
        pattern = getattr(name, "pattern", "")
        if role == "button" and "Next" in pattern:
            return FakeLocator(count=1, text="Next")
        return FakeLocator()

    def inner_text(self, **_kwargs):
        return "Resume\nruntime_unique_resume.txt\nPlease upload an acceptable document format (DOC, DOCX, PDF). Change file\nNext"


class GlobalNextTrapPage(FakeEvidencePage):
    def __init__(self) -> None:
        self.global_next_calls = 0

    def get_by_role(self, role, name=None):
        pattern = getattr(name, "pattern", "")
        if role == "button" and "Easy Apply" in pattern:
            return FakeLocator(count=1, text="Easy Apply")
        if role == "button" and ("Next" in pattern or "Review" in pattern):
            self.global_next_calls += 1
            return FakeLocator(count=1, text="Next")
        return FakeLocator()

    def locator(self, selector):
        if selector in {
            ".jobs-easy-apply-modal",
            "[role='dialog'][aria-modal='true']",
            "[data-test-modal]",
        }:
            return EmptyEasyApplyModal()
        if selector == "button:visible, a[role='button']:visible":
            return FakeLocator(texts=["Easy Apply", "Next"])
        return FakeLocator()


class FakeDialogLocator(FakeLocator):
    def __init__(self, dialogs):
        super().__init__(count=len(dialogs))
        self.dialogs = dialogs

    def nth(self, index):
        return self.dialogs[index]


class FakeDialog(FakeLocator):
    def __init__(self, title: str, text: str) -> None:
        super().__init__(count=1, text=text)
        self.title = title

    def locator(self, selector):
        if selector in {"h1:visible", "h2:visible", "h3:visible", "[role='heading']:visible"} and self.title:
            return FakeLocator(count=1, text=self.title)
        return FakeLocator()


class FakeEvidenceContext:
    def __init__(self) -> None:
        self.page = FakeEvidencePage()

    def new_page(self):
        return self.page

    @property
    def pages(self):
        return [self.page]


class FakeCardClickPage(FakeEvidencePage):
    url = "https://www.linkedin.com/jobs/search/?currentJobId=4276543210"

    def locator(self, selector):
        if "4276543210" in selector:
            return FakeLocator(
                count=1,
                attributes={
                    "class": "jobs-search-results__list-item selected",
                    "aria-selected": "true",
                },
            )
        return FakeLocator()


class FakeDiagnosticPage(FakeEvidencePage):
    def __init__(
        self,
        *,
        easy_apply=False,
        generic_apply=False,
        external_href="",
        url="https://www.linkedin.com/jobs/view/123",
    ) -> None:
        self.easy_apply = easy_apply
        self.generic_apply = generic_apply
        self.external_href = external_href
        self.url = url

    def get_by_role(self, role, name=None):
        pattern = getattr(name, "pattern", "")
        if role != "button":
            return FakeLocator()
        if "Easy Apply" in pattern and self.easy_apply:
            return FakeLocator(count=1, text="Easy Apply")
        if pattern == r"^\s*Apply\s*$" and self.generic_apply:
            return FakeLocator(count=1, text="Apply")
        return FakeLocator()

    def locator(self, selector):
        if selector == 'button:has-text("Easy Apply")' and self.easy_apply:
            return FakeLocator(count=1, text="Easy Apply")
        if selector == 'button:has-text("Apply")' and self.generic_apply:
            return FakeLocator(count=1, text="Apply")
        if selector == "button:visible, a[role='button']:visible":
            texts = []
            if self.easy_apply:
                texts.append("Easy Apply")
            if self.generic_apply:
                texts.append("Apply")
            return FakeLocator(texts=texts)
        if selector.startswith("a[href]:visible") and self.external_href:
            return FakeLocator(count=1, text="Apply", href=self.external_href)
        return FakeLocator()


class FakeContactField(FakeLocator):
    def __init__(
        self,
        label,
        *,
        required=True,
        value="",
        field_type="text",
        tag="input",
    ) -> None:
        super().__init__(count=1, text=label)
        self.label = label
        self.required = required
        self.value = value
        self.filled_values: list[str] = []
        self.selected_values: list[str] = []
        self.pressed_keys: list[str] = []
        self.clicked = False
        self.field_type = field_type
        self.tag = tag

    def evaluate(self, expression, arg=None):
        if "fallback" in expression:
            return self.label
        if "field_type" in expression:
            return {
                "tag": self.tag,
                "type": self.field_type,
                "field_type": "select" if self.tag == "select" else self.field_type,
                "required": self.required,
                "has_value": bool(str(self.value).strip()),
            }
        if "autocomplete" in expression:
            return f"{self.label} {self.field_type}"
        return self.value

    def fill(self, value):
        self.value = value
        self.filled_values.append(value)

    def select_option(self, label=None, value=None):
        selected = label or value
        self.value = selected
        self.selected_values.append(selected)

    def click(self, **_kwargs):
        self.clicked = True
        self.value = self.label

    def check(self, **_kwargs):
        self.click()

    def press(self, key, **_kwargs):
        self.pressed_keys.append(key)


class FakePlaceholderSelectField(FakeContactField):
    def __init__(self, label) -> None:
        super().__init__(
            label,
            required=True,
            value="Select an option",
            field_type="select",
            tag="select",
        )

    def evaluate(self, expression, arg=None):
        if "field_type" in expression:
            return {
                "tag": "select",
                "type": "select-one",
                "field_type": "select",
                "required": True,
                "has_value": False,
            }
        return super().evaluate(expression, arg)


class FakeContactFields(FakeLocator):
    def __init__(self, fields):
        super().__init__(count=len(fields))
        self.fields = fields

    def count(self):
        return len(self.fields)

    def nth(self, index):
        return self.fields[index]


class FakeContactModal(FakeEasyApplyModal):
    def __init__(self, fields):
        super().__init__()
        self.fields = fields

    def locator(self, selector):
        if selector == "input:visible, textarea:visible, select:visible":
            return FakeContactFields(self.fields)
        if selector == "input[required]:visible, textarea[required]:visible, select[required]:visible":
            return FakeContactFields([field for field in self.fields if field.required])
        return super().locator(selector)


class FakeScreeningRadioField(FakeContactField):
    def __init__(
        self,
        question: str,
        options: list[str],
        strategy: str = "label_click",
        accepted: bool = True,
        validation_after: str = "",
        group_diagnostics: dict | None = None,
        expected_question: str | None = None,
        recovery_succeeds: bool = False,
    ) -> None:
        super().__init__(question, required=True, value="", field_type="radio")
        self.question = question
        self.options = options
        self.selected_option: str | None = None
        self.strategy = strategy
        self.accepted = accepted
        self.validation_after = validation_after
        self.group_diagnostics = group_diagnostics or {
            "question_text": question,
            "group_outer_html": "<fieldset><legend>{}</legend></fieldset>".format(question),
            "provider_name": "linkedin_native",
            "inputs_before": [
                {"id": f"{question}-{option}-before", "name": question, "value": option, "label": option, "checked": False}
                for option in options
            ],
            "inputs_after": [
                {"id": f"{question}-{option}-after", "name": question, "value": option, "label": option, "checked": False}
                for option in options
            ],
            "active_element_after": {},
            "selected_input_outer_html": "<input type='radio'>",
            "exact_label_clicked": "<label>{}</label>".format(options[0] if options else ""),
        }
        self.expected_question = expected_question
        self.last_payload: dict | None = None
        self.recovery_succeeds = recovery_succeeds

    def evaluate(self, expression, arg=None):
        if "radioWriterPayload" in expression:
            self.last_payload = arg or {}
            if self.expected_question is not None and self.last_payload.get("question") != self.expected_question:
                raise AssertionError("Radio writer was not scoped to the expected question.")
            answer = str((arg or {}).get("answer") or "").lower()
            for option in self.options:
                if option.lower() == answer:
                    if self.recovery_succeeds:
                        self.selected_option = option
                        self.value = option
                        return {
                            "success": True,
                            "final_write_status": "success",
                            "option_labels_found": self.options,
                            "selected_option_label": option,
                            "click_strategy_used": "post_selection_validation_recovery",
                            "checked_state_before": False,
                            "aria_state_before": "",
                            "checked_state_after": True,
                            "aria_state_after": "true",
                            "validation_message_before": "Please make a selection",
                            "validation_message_after": "",
                            "attempts": [
                                {
                                    "strategy": "label_click",
                                    "checked": True,
                                    "aria_checked": "true",
                                    "validation_message": "Please make a selection",
                                    "accepted": False,
                                },
                                {
                                    "strategy": "post_selection_validation_recovery",
                                    "checked": True,
                                    "aria_checked": "true",
                                    "validation_message": "",
                                    "accepted": True,
                                },
                            ],
                            "group_diagnostics": self.group_diagnostics,
                            "reason": None,
                        }
                    if self.accepted:
                        self.selected_option = option
                        self.value = option
                    return {
                        "success": self.accepted,
                        "final_write_status": "success" if self.accepted else "failed",
                        "option_labels_found": self.options,
                        "selected_option_label": option,
                        "click_strategy_used": self.strategy if self.accepted else None,
                        "checked_state_before": False,
                        "aria_state_before": "",
                        "checked_state_after": self.accepted,
                        "aria_state_after": "true" if self.accepted else "",
                        "validation_message_before": "Please make a selection",
                        "validation_message_after": self.validation_after,
                        "attempts": [
                            {
                                "strategy": self.strategy,
                                "checked": self.accepted,
                                "aria_checked": "true" if self.accepted else "",
                                "validation_message": self.validation_after,
                                "accepted": self.accepted,
                            }
                        ],
                        "group_diagnostics": self.group_diagnostics,
                        "reason": None if self.accepted else "Radio option was clicked but LinkedIn did not accept the selection.",
                    }
            return {
                "success": False,
                "final_write_status": "failed",
                "option_labels_found": self.options,
                "group_diagnostics": self.group_diagnostics,
                "reason": "No radio option matched answer.",
            }
        if "const roots" in expression and "querySelectorAll" in expression:
            return self.options
        if "questionRoot" in expression:
            return self.question
        if "Array.from(group" in expression:
            return self.options
        return super().evaluate(expression, arg)


class SingleJobApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        FakeApplicationAutomation.processed_job_ids = []
        FakeApplicationAutomation.processed_resume_ids = []
        FakeApplicationAutomation.processed_resume_paths = []
        self.db = SessionLocal()
        set_active_portal(self.db, "linkedin")
        token = uuid4().hex
        self.resume = ResumeProfile(
            filename="candidate.pdf",
            stored_path="candidate.pdf",
            raw_text="Python data science",
            skills=["python"],
            experience=[],
            education=[],
            job_titles=[],
            keywords=[],
            technologies=["Python"],
            domain_expertise=["Data Science"],
            detected_role="Data Scientist",
            role_suggestions=[],
            generated_search_keywords=["Data Scientist"],
            generated_search_query="Data Scientist",
            role_confidence=80,
        )
        self.db.add(self.resume)
        self.db.commit()
        self.db.refresh(self.resume)
        self.jobs = [
            JobOpportunity(
                resume_id=self.resume.id,
                title=f"Job {index}",
                company="Example",
                location="Remote",
                job_url=f"https://example.com/{token}/{index}",
                description="Python",
                easy_apply=True,
                match_score=100 - index,
            )
            for index in range(3)
        ]
        self.db.add_all(self.jobs)
        self.db.commit()
        for job in self.jobs:
            self.db.refresh(job)

    def _ensure_job_count(self, count: int) -> None:
        if len(self.jobs) >= count:
            return
        token = uuid4().hex
        new_jobs = [
            JobOpportunity(
                resume_id=self.resume.id,
                title=f"Extra Job {index}",
                company="Example",
                location="Remote",
                job_url=f"https://example.com/{token}/{index}",
                description="Python",
                easy_apply=True,
                match_score=max(1, 100 - index),
                priority_score=max(1, 100 - index),
                completion_likelihood_score=90,
                application_friction_score=10,
                freshness_score=max(1, 100 - index),
            )
            for index in range(len(self.jobs), count)
        ]
        self.db.add_all(new_jobs)
        self.db.commit()
        for job in new_jobs:
            self.db.refresh(job)
        self.jobs.extend(new_jobs)

    def tearDown(self) -> None:
        job_ids = list(
            self.db.scalars(
                select(JobOpportunity.id).where(JobOpportunity.resume_id == self.resume.id)
            ).all()
        )
        if job_ids:
            self.db.execute(delete(ApplicationRecord).where(ApplicationRecord.job_id.in_(job_ids)))
        self.db.execute(delete(ApplyTopBatch).where(ApplyTopBatch.resume_id == self.resume.id))
        self.db.execute(delete(CandidateEmploymentHistory).where(CandidateEmploymentHistory.resume_id == self.resume.id))
        self.db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id))
        self.db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == self.resume.id))
        self.db.delete(self.resume)
        self.db.commit()
        self.db.close()

    def test_apply_selected_processes_exactly_one_job(self) -> None:
        selected = self.jobs[1]
        request = ApplySelectedRequest(
            resume_id=self.resume.id,
            job_id=selected.id,
            dry_run=True,
        )
        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            record = apply_selected_job(request, self.db)

        self.assertEqual(FakeApplicationAutomation.processed_job_ids, [selected.id])
        self.assertEqual(record.job_id, selected.id)
        self.assertTrue(record.dry_run)
        self.assertEqual(record.current_step, "dry_run_complete")
        self.assertEqual(record.evidence_path, "outputs/live_test/test")
        self.assertEqual(FakeApplicationAutomation.processed_resume_ids, [self.resume.id])
        self.assertEqual(FakeApplicationAutomation.processed_resume_paths, [self.resume.stored_path])

    def test_apply_selected_dice_job_routes_to_dice_adapter(self) -> None:
        set_active_portal(self.db, "dice")
        self.resume.portal = "dice"
        self.db.add(self.resume)
        self.db.commit()
        dice_job = JobOpportunity(
            resume_id=self.resume.id,
            portal="dice",
            title="Dice Data Analyst",
            company="Dice Co",
            location="Remote",
            job_url=f"https://www.dice.com/job-detail/{uuid4().hex}",
            description="Python SQL",
            easy_apply=True,
            match_score=95,
        )
        self.db.add(dice_job)
        self.db.commit()
        self.db.refresh(dice_job)

        # External ATS -> Dice dry-run classifies as unsupported_form (never submits).
        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation), patch.object(
            DicePortalAdapter, "_apply_browser_context", _fake_dice_ctx
        ), patch.object(
            DicePortalAdapter, "_detect_apply_form",
            return_value={"apply_type": "external", "fields": [], "submit_present": False},
        ):
            record = apply_selected_job(
                ApplySelectedRequest(resume_id=self.resume.id, job_id=dice_job.id, dry_run=True),
                self.db,
            )

        self.assertEqual(record.portal, "dice")
        self.assertEqual(record.status, ApplicationStatus.UNSUPPORTED_FORM)
        self.assertEqual(record.current_step, "dice_apply_unsupported_form")
        # LinkedIn automation was NOT used for a Dice job (portal isolation).
        self.assertEqual(FakeApplicationAutomation.processed_job_ids, [])

    def test_apply_selected_rejects_wrong_resume_id(self) -> None:
        other_resume = ResumeProfile(
            filename="other.pdf",
            stored_path="other.pdf",
            raw_text="Other candidate",
            skills=[],
            experience=[],
            education=[],
            job_titles=[],
            keywords=[],
            technologies=[],
            domain_expertise=[],
            detected_role="Analyst",
            role_suggestions=[],
            generated_search_keywords=[],
            generated_search_query=None,
            role_confidence=0,
        )
        self.db.add(other_resume)
        self.db.commit()
        self.db.refresh(other_resume)
        try:
            with self.assertRaises(HTTPException):
                apply_selected_job(
                    ApplySelectedRequest(
                        resume_id=other_resume.id,
                        job_id=self.jobs[0].id,
                        dry_run=True,
                    ),
                    self.db,
                )
            self.assertEqual(FakeApplicationAutomation.processed_job_ids, [])
        finally:
            self.db.delete(other_resume)
            self.db.commit()

    def test_apply_top_behavior_remains_ranked_and_multi_job(self) -> None:
        request = ApplyRequest(
            resume_id=self.resume.id,
            limit=2,
            dry_run=True,
        )
        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            records = apply_top_jobs(request, self.db)

        expected_ids = [self.jobs[0].id, self.jobs[1].id]
        self.assertEqual(FakeApplicationAutomation.processed_job_ids, expected_ids)
        self.assertEqual([record.job_id for record in records], expected_ids)
        self.assertTrue(all(record.evidence_path is not None for record in records))
        self.assertEqual(FakeApplicationAutomation.processed_resume_ids, [self.resume.id, self.resume.id])
        self.assertEqual(FakeApplicationAutomation.processed_resume_paths, [self.resume.stored_path, self.resume.stored_path])

    def test_apply_top_dice_portal_filters_to_dice_jobs(self) -> None:
        set_active_portal(self.db, "dice")
        self.resume.portal = "dice"
        self.db.add(self.resume)
        self.db.commit()
        dice_job = JobOpportunity(
            resume_id=self.resume.id,
            portal="dice",
            title="Dice Data Analyst",
            company="Dice Co",
            location="Remote",
            job_url=f"https://www.dice.com/job-detail/{uuid4().hex}",
            description="Python SQL",
            easy_apply=True,
            match_score=99,
            priority_score=99,
            completion_likelihood_score=80,
            application_friction_score=20,
        )
        self.db.add(dice_job)
        self.db.commit()
        self.db.refresh(dice_job)

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation), patch.object(
            DicePortalAdapter, "_apply_browser_context", _fake_dice_ctx
        ), patch.object(
            DicePortalAdapter, "_detect_apply_form",
            return_value={"apply_type": "external", "fields": [], "submit_present": False},
        ):
            records = apply_top_jobs(
                ApplyRequest(resume_id=self.resume.id, limit=5, dry_run=True, portal="dice"),
                self.db,
            )

        self.assertEqual([record.job_id for record in records], [dice_job.id])
        self.assertEqual(records[0].portal, "dice")
        self.assertEqual(records[0].status, ApplicationStatus.UNSUPPORTED_FORM)
        self.assertEqual(FakeApplicationAutomation.processed_job_ids, [])

    def test_apply_top_20_selects_20_jobs(self) -> None:
        self._ensure_job_count(25)

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            records = apply_top_jobs(ApplyRequest(resume_id=self.resume.id, limit=20, dry_run=True), self.db)

        self.assertEqual(len(records), 20)
        self.assertEqual(len(FakeApplicationAutomation.processed_job_ids), 20)

    def test_apply_all_selects_all_ranked_jobs_up_to_safety_cap(self) -> None:
        self._ensure_job_count(60)

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            records = apply_top_jobs(ApplyRequest(resume_id=self.resume.id, limit=999, dry_run=True), self.db)

        self.assertEqual(len(records), 50)
        self.assertEqual(len(FakeApplicationAutomation.processed_job_ids), 50)

    def test_apply_top_orders_jobs_by_final_apply_priority(self) -> None:
        self.jobs[0].match_score = 95
        self.jobs[0].priority_score = 62
        self.jobs[0].completion_likelihood_score = 30
        self.jobs[0].application_friction_score = 70
        self.jobs[0].recommended_apply_priority = "Complex"
        self.jobs[1].match_score = 76
        self.jobs[1].priority_score = 88
        self.jobs[1].completion_likelihood_score = 92
        self.jobs[1].application_friction_score = 8
        self.jobs[1].recommended_apply_priority = "Simple"
        self.jobs[2].match_score = 80
        self.jobs[2].priority_score = 74
        self.jobs[2].completion_likelihood_score = 65
        self.jobs[2].application_friction_score = 35
        self.jobs[2].recommended_apply_priority = "Medium"
        self.db.add_all(self.jobs)
        self.db.commit()

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            records = apply_top_jobs(ApplyRequest(resume_id=self.resume.id, limit=3, dry_run=True), self.db)

        expected_ids = [self.jobs[1].id, self.jobs[2].id, self.jobs[0].id]
        self.assertEqual(FakeApplicationAutomation.processed_job_ids, expected_ids)
        self.assertEqual([record.job_id for record in records], expected_ids)

    def test_apply_top_defaults_to_latest_search_batch(self) -> None:
        old_batch = JobSearchBatch(
            id=str(uuid4()),
            resume_id=self.resume.id,
            search_query="Old search",
            location="Remote",
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        latest_batch = JobSearchBatch(
            id=str(uuid4()),
            resume_id=self.resume.id,
            search_query="Latest search",
            location="Remote",
            created_at=datetime.utcnow(),
        )
        self.db.add_all([old_batch, latest_batch])
        self.db.commit()
        old_job = JobOpportunity(
            resume_id=self.resume.id,
            search_batch_id=old_batch.id,
            search_timestamp=old_batch.created_at,
            title="Old Batch Job",
            company="Example",
            location="Remote",
            job_url=f"https://example.com/old/{uuid4().hex}",
            description="Python",
            easy_apply=True,
            match_score=99,
        )
        latest_job = JobOpportunity(
            resume_id=self.resume.id,
            search_batch_id=latest_batch.id,
            search_timestamp=latest_batch.created_at,
            title="Latest Batch Job",
            company="Example",
            location="Remote",
            job_url=f"https://example.com/latest/{uuid4().hex}",
            description="Python",
            easy_apply=True,
            match_score=70,
        )
        self.db.add_all([old_job, latest_job])
        self.db.commit()
        self.db.refresh(old_job)
        self.db.refresh(latest_job)

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            records = apply_top_jobs(
                ApplyRequest(resume_id=self.resume.id, limit=10, dry_run=True),
                self.db,
            )

        self.assertEqual(FakeApplicationAutomation.processed_job_ids, [latest_job.id])
        self.assertEqual([record.job_id for record in records], [latest_job.id])

    def test_apply_top_background_batch_can_be_retrieved_after_start(self) -> None:
        self._ensure_job_count(12)

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            batch = _create_apply_top_batch(
                ApplyRequest(resume_id=self.resume.id, limit=10, dry_run=True),
                self.db,
            )
            _run_apply_top_batch(batch.id)

        self.db.expire_all()
        refreshed = self.db.get(ApplyTopBatch, batch.id)

        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.limit, 10)
        self.assertEqual(refreshed.completed_count, 10)
        self.assertEqual(len(refreshed.job_results), 10)
        self.assertTrue(refreshed.dry_run)
        self.assertTrue(Path(refreshed.debug_path, "batch_status.json").exists())

    def test_apply_top_background_batch_supports_top_20(self) -> None:
        self._ensure_job_count(25)

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            batch = _create_apply_top_batch(
                ApplyRequest(resume_id=self.resume.id, limit=20, dry_run=True),
                self.db,
            )
            _run_apply_top_batch(batch.id)

        self.db.expire_all()
        refreshed = self.db.get(ApplyTopBatch, batch.id)

        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.limit, 20)
        self.assertEqual(refreshed.completed_count, 20)
        self.assertEqual(len(refreshed.job_results), 20)

    def test_apply_top_background_batch_supports_apply_all_safety_cap(self) -> None:
        self._ensure_job_count(60)

        with patch("app.main.LinkedInAutomation", FakeApplicationAutomation):
            batch = _create_apply_top_batch(
                ApplyRequest(resume_id=self.resume.id, limit=999, dry_run=True),
                self.db,
            )
            _run_apply_top_batch(batch.id)

        self.db.expire_all()
        refreshed = self.db.get(ApplyTopBatch, batch.id)

        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.limit, 50)
        self.assertEqual(refreshed.completed_count, 50)
        self.assertEqual(len(refreshed.job_results), 50)

    def test_apply_top_background_batch_continues_after_one_job_failure(self) -> None:
        FailingSecondJobAutomation.processed_job_ids = []
        with patch("app.main.LinkedInAutomation", FailingSecondJobAutomation):
            batch = _create_apply_top_batch(
                ApplyRequest(resume_id=self.resume.id, limit=3, dry_run=True),
                self.db,
            )
            _run_apply_top_batch(batch.id)

        self.db.expire_all()
        refreshed = self.db.get(ApplyTopBatch, batch.id)

        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.completed_count, 3)
        self.assertEqual(refreshed.failed_count, 1)
        self.assertEqual(FailingSecondJobAutomation.processed_job_ids, [self.jobs[0].id, self.jobs[1].id, self.jobs[2].id])

    def test_apply_top_background_batch_continues_after_radio_write_failure(self) -> None:
        RadioFailureSecondJobAutomation.processed_job_ids = []
        with patch("app.main.LinkedInAutomation", RadioFailureSecondJobAutomation):
            batch = _create_apply_top_batch(
                ApplyRequest(resume_id=self.resume.id, limit=3, dry_run=True),
                self.db,
            )
            _run_apply_top_batch(batch.id)

        self.db.expire_all()
        refreshed = self.db.get(ApplyTopBatch, batch.id)

        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.completed_count, 3)
        self.assertEqual(refreshed.failed_count, 1)
        self.assertIn("did not accept", refreshed.job_results[1]["error"])
        self.assertEqual(RadioFailureSecondJobAutomation.processed_job_ids, [self.jobs[0].id, self.jobs[1].id, self.jobs[2].id])

    def test_apply_top_batch_records_timed_out_job_result(self) -> None:
        record = ApplicationRecord(
            job_id=self.jobs[0].id,
            status=ApplicationStatus.FAILED,
            dry_run=True,
            current_step="timed_out",
            error_message="Application timed out while processing the job.",
        )

        status = _batch_job_status(record)

        self.assertEqual(status, "timed_out")

    def test_selected_dry_run_writes_logs_and_step_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            resume_path = temporary_path / "resume.pdf"
            resume_path.write_bytes(b"resume")
            settings = Settings(
                openai_api_key=None,
                live_test_output_dir=temporary_path / "live_test",
                easy_apply_debug_dir=temporary_path / "easy_apply_debug",
                linkedin_storage_state=temporary_path / "state.json",
            )
            automation = LinkedInAutomation(settings)
            context = FakeEvidenceContext()

            @contextmanager
            def fake_browser_context():
                yield context

            resume = self.resume
            resume.stored_path = str(resume_path)
            with patch.object(automation, "_browser_context", fake_browser_context):
                result = automation.apply_to_job(
                    self.jobs[0],
                    resume,
                    CandidateProfile(
                        first_name="Ava",
                        last_name="Shah",
                        email="ava@example.com",
                        mobile_phone="5551234567",
                        phone_country_code="+1",
                        current_location="New York, NY",
                        linkedin_url="https://www.linkedin.com/in/ava",
                    ),
                    dry_run=True,
                    capture_evidence=True,
                )

            evidence_path = Path(result.evidence_path)
            # A dry run that reaches the submit/review stage is a successful completed run.
            self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
            self.assertEqual(result.current_step, "dry_run_complete")
            self.assertTrue((evidence_path / "application_log.json").exists())
            screenshots = settings.live_test_output_dir / "screenshots" / evidence_path.name
            self.assertTrue((screenshots / "01_job_page.png").exists())
            self.assertTrue((screenshots / "02_easy_apply_modal.png").exists())
            self.assertTrue((screenshots / "03_resume_upload.png").exists())
            self.assertTrue((screenshots / "04_review_step.png").exists())
            debug_files = list((settings.easy_apply_debug_dir).glob("*/*/diagnostic.json"))
            self.assertEqual(len(debug_files), 1)
            diagnostics = __import__("json").loads(
                debug_files[0].read_text(encoding="utf-8")
            )
            self.assertTrue(diagnostics["search_phase_easy_apply_result"])
            self.assertTrue(diagnostics["application_phase_easy_apply_result"])
            self.assertFalse(diagnostics["EASY_APPLY_MISMATCH"])
            self.assertIn("navigation_events", diagnostics)

    def test_missing_resume_file_causes_needs_review_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            missing_path = temporary_path / "missing_unique_resume.pdf"
            self.resume.filename = "missing_unique_resume.pdf"
            self.resume.stored_path = str(missing_path)
            automation = LinkedInAutomation(
                Settings(
                    openai_api_key=None,
                    resume_upload_debug_dir=temporary_path / "resume_upload_debug",
                    linkedin_storage_state=temporary_path / "state.json",
                )
            )

            result = automation.apply_to_job(
                self.jobs[0],
                self.resume,
                None,
                dry_run=True,
                capture_evidence=False,
            )

            self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
            self.assertEqual(result.current_step, "needs_review")
            self.assertIn("missing", result.message.lower())
            self.assertFalse(result.resume_upload_diagnostics["file_exists"])
            self.assertIsNone(result.resume_upload_diagnostics["file_passed_to_playwright"])
            diagnostic_files = list((temporary_path / "resume_upload_debug").glob("*/*/resume_upload_check.json"))
            self.assertEqual(len(diagnostic_files), 1)

    def test_resume_upload_diagnostics_record_active_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            resume_path = temporary_path / "unique_candidate_resume.pdf"
            resume_path.write_bytes(b"unique resume bytes")
            self.resume.filename = "unique_candidate_resume.pdf"
            self.resume.stored_path = str(resume_path)
            automation = LinkedInAutomation(
                Settings(
                    openai_api_key=None,
                    resume_upload_debug_dir=temporary_path / "resume_upload_debug",
                    linkedin_storage_state=temporary_path / "state.json",
                )
            )

            debug_dir, diagnostics = automation._create_resume_upload_diagnostics(
                self.jobs[0],
                self.resume,
            )

            self.assertEqual(diagnostics["active_resume_id"], self.resume.id)
            self.assertEqual(diagnostics["job_resume_id"], self.jobs[0].resume_id)
            self.assertEqual(diagnostics["resume_original_filename"], "unique_candidate_resume.pdf")
            self.assertEqual(diagnostics["file_passed_to_playwright"], str(resume_path.resolve()))
            self.assertTrue(diagnostics["file_exists"])
            self.assertFalse(diagnostics["mismatch_detected"])
            self.assertTrue((debug_dir / "resume_upload_check.json").exists())

    def test_playwright_upload_payload_uses_original_resume_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            stored_path = temporary_path / "uuid-storage-name.txt"
            stored_path.write_bytes(b"resume bytes")
            automation = LinkedInAutomation(Settings(openai_api_key=None))

            payload = automation._resume_upload_payload(
                stored_path,
                {"resume_original_filename": "original_candidate_resume.txt"},
            )

            self.assertEqual(payload["name"], "original_candidate_resume.txt")
            self.assertEqual(payload["mimeType"], "text/plain")
            self.assertEqual(payload["buffer"], b"resume bytes")

    def test_easy_apply_diagnostics_confirm_explicit_control(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        page = FakeDiagnosticPage(easy_apply=True)
        locator, diagnostics = automation._detect_easy_apply(
            page,
            self.jobs[0],
            {"networkidle": True, "main_content": True},
        )

        self.assertIsNotNone(locator)
        self.assertEqual(diagnostics["classification"], "linkedin_easy_apply")
        self.assertIn("Easy Apply", diagnostics["visible_button_texts"])
        self.assertEqual(
            next(
                result["count"]
                for result in diagnostics["selector_results"]
                if result["name"] == "role_button_easy_apply"
            ),
            1,
        )
        exact_matches = {
            item["selector"]: item["count"]
            for item in diagnostics["exact_selector_matches"]
        }
        self.assertEqual(exact_matches['button:has-text("Easy Apply")'], 1)
        self.assertIn('button:has-text("Apply")', exact_matches)

    def test_form_navigation_is_scoped_to_easy_apply_modal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            resume_path = temporary_path / "resume.pdf"
            resume_path.write_bytes(b"resume")
            automation = LinkedInAutomation(
                Settings(
                    openai_api_key=None,
                    live_test_output_dir=temporary_path / "live_test",
                    easy_apply_debug_dir=temporary_path / "easy_apply_debug",
                    linkedin_storage_state=temporary_path / "state.json",
                )
            )
            page = GlobalNextTrapPage()
            context = FakeEvidenceContext()
            context.page = page

            @contextmanager
            def fake_browser_context():
                yield context

            self.resume.stored_path = str(resume_path)
            with patch.object(automation, "_browser_context", fake_browser_context):
                result = automation.apply_to_job(
                    self.jobs[0],
                    self.resume,
                    None,
                    dry_run=True,
                    capture_evidence=True,
                )

            self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
            self.assertEqual(result.current_step, "needs_review")
            self.assertEqual(page.global_next_calls, 0)

    def test_easy_apply_modal_is_preferred_over_feedback_dialog(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        feedback_dialog = FakeDialog(
            "Help us improve by sharing feedback",
            "Help us improve by sharing feedback\nQuestion 1 of 4\nNext",
        )
        easy_apply_dialog = FakeDialog(
            "Apply to Claroda",
            "Apply to Claroda\nFirst name\nLast name\nLocation (city)\nNext",
        )

        modal = automation._best_easy_apply_modal(FakeDialogLocator([feedback_dialog, easy_apply_dialog]))

        self.assertIs(modal, easy_apply_dialog)

    def test_external_apply_link_is_not_treated_as_easy_apply(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        page = FakeDiagnosticPage(
            generic_apply=True,
            external_href="https://careers.example.org/apply/123",
        )
        locator, diagnostics = automation._detect_easy_apply(
            page,
            self.jobs[0],
            {"networkidle": True, "main_content": True},
        )

        self.assertIsNone(locator)
        self.assertEqual(diagnostics["classification"], "external_application")
        self.assertEqual(
            diagnostics["external_apply_links"][0]["host"],
            "careers.example.org",
        )

    def test_linkedin_job_id_is_extracted_from_job_url(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        self.assertEqual(
            automation._linkedin_job_id(
                "https://www.linkedin.com/jobs/view/data-scientist-4276543210/?trackingId=x"
            ),
            "4276543210",
        )

    def test_job_card_click_records_active_in_place_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            automation = LinkedInAutomation(
                Settings(
                    openai_api_key=None,
                    easy_apply_debug_dir=Path(temporary_directory),
                )
            )
            page = FakeCardClickPage()
            context = FakeEvidenceContext()
            context.page = page
            card = FakeLocator(
                attributes={
                    "class": "jobs-search-results__list-item",
                    "data-occludable-job-id": "4276543210",
                }
            )
            link = FakeLocator(
                count=1,
                href="https://www.linkedin.com/jobs/view/4276543210/",
            )
            debug_dir = Path(temporary_directory) / "run" / "4276543210"
            debug_dir.mkdir(parents=True)

            diagnostic = automation._inspect_job_card_click(
                context,
                page,
                card,
                link,
                "https://www.linkedin.com/jobs/view/4276543210/",
                "4276543210",
                debug_dir,
            )

            self.assertEqual(diagnostic["opening_mode"], "in_place_details_panel")
            self.assertTrue(diagnostic["selected_job_card_active_after_click"])
            self.assertEqual(
                diagnostic["navigation_events"][1]["page_title"],
                "Fake LinkedIn Job",
            )
            self.assertTrue((debug_dir / "02_job_details_page_loaded.png").exists())

    def test_mobile_phone_required_is_filled_from_candidate_profile(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        phone = FakeContactField("Mobile phone number", required=True)
        modal = FakeContactModal([phone])
        events: list[dict] = []
        missing = automation._fill_contact_fields(
            modal,
            CandidateProfile(mobile_phone="5551234567"),
            self.resume,
            None,
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertEqual(phone.filled_values, ["5551234567"])
        self.assertEqual(events[0]["action"], "filled")

    def test_country_code_dropdown_is_filled_from_candidate_profile(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        country = FakeContactField(
            "Phone country code",
            required=True,
            tag="select",
            field_type="select-one",
        )
        modal = FakeContactModal([country])
        events: list[dict] = []
        missing = automation._fill_contact_fields(
            modal,
            CandidateProfile(phone_country_code="+91"),
            self.resume,
            None,
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertEqual(country.selected_values, ["+91"])

    def test_existing_email_is_not_overwritten(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        email = FakeContactField("Email address", required=True, value="existing@example.com")
        modal = FakeContactModal([email])
        events: list[dict] = []
        missing = automation._fill_contact_fields(
            modal,
            CandidateProfile(email="new@example.com"),
            self.resume,
            None,
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertEqual(email.value, "existing@example.com")
        self.assertEqual(email.filled_values, [])
        self.assertEqual(events[0]["action"], "kept_existing_value")

    def test_missing_phone_number_causes_needs_review_field(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        phone = FakeContactField("Mobile phone number", required=True)
        modal = FakeContactModal([phone])
        events: list[dict] = []
        missing = automation._fill_contact_fields(
            modal,
            CandidateProfile(),
            self.resume,
            None,
            None,
            events,
        )

        self.assertEqual(missing, ["Mobile phone number"])
        self.assertEqual(events[0]["action"], "missing_candidate_profile_value")

    def test_location_city_is_filled_from_resume_when_candidate_profile_is_empty(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        location = FakeContactField("Location (city)", required=True)
        modal = FakeContactModal([location])
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Rishith Shetty\nMumbai, India\nData analyst with Python and Power BI.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        events: list[dict] = []

        missing = automation._fill_contact_fields(
            modal,
            CandidateProfile(),
            resume,
            None,
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertEqual(location.filled_values, ["Mumbai"])
        self.assertEqual(location.pressed_keys, ["ArrowDown", "Enter"])
        self.assertEqual(events[0]["contact_key"], "current_location")
        self.assertEqual(events[0]["value_source"], "Resume Visible Text")
        self.assertEqual(events[0]["value_found"], "Mumbai")
        self.assertEqual(events[0]["action"], "filled_typeahead")

    def test_location_city_prefers_linkedin_modal_header_location(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        location = FakeContactField("Location (city)", required=True)
        modal = FakeContactModal([location])
        modal._text = "Apply to Claroda\nRishith Shetty\nMumbai, Maharashtra, India\nLocation (city)"
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Rishith Shetty\nMumbai, India\nData analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        events: list[dict] = []

        missing = automation._fill_contact_fields(modal, CandidateProfile(), resume, None, None, events)

        self.assertEqual(missing, [])
        self.assertEqual(location.filled_values, ["Mumbai, Maharashtra, India"])
        self.assertEqual(events[0]["value_source"], "LinkedIn Easy Apply Header")

    def test_dry_run_continues_after_contact_fields_are_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            automation = LinkedInAutomation(
                Settings(
                    openai_api_key=None,
                    live_test_output_dir=Path(temporary_directory) / "live_test",
                    easy_apply_debug_dir=Path(temporary_directory) / "easy_apply_debug",
                    contact_debug_dir=Path(temporary_directory) / "contact_debug",
                    linkedin_storage_state=Path(temporary_directory) / "state.json",
                )
            )
            phone = FakeContactField("Mobile phone number", required=True)
            page = FakeEvidencePage()
            context = FakeEvidenceContext()
            context.page = page

            def fake_modal(_page):
                return FakeContactModal([phone])

            @contextmanager
            def fake_browser_context():
                yield context

            resume_path = Path(temporary_directory) / "resume.pdf"
            resume_path.write_bytes(b"resume")
            self.resume.stored_path = str(resume_path)
            with (
                patch.object(automation, "_browser_context", fake_browser_context),
                patch.object(automation, "_current_easy_apply_modal", fake_modal),
                patch.object(automation, "_wait_for_easy_apply_modal", fake_modal),
            ):
                result = automation.apply_to_job(
                    self.jobs[0],
                    self.resume,
                    CandidateProfile(mobile_phone="5551234567"),
                    dry_run=True,
                    capture_evidence=True,
                )

            self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
            self.assertEqual(result.current_step, "dry_run_complete")
            self.assertEqual(phone.filled_values, ["5551234567"])
            self.assertEqual(result.missing_contact_fields, [])
            self.assertEqual(
                result.contact_fields_filled[0]["field_label"],
                "Mobile phone number",
            )

    def test_continue_button_is_treated_as_navigation(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        navigation = automation._find_navigation_button(ContinueEasyApplyModal())

        self.assertEqual(navigation["text"], "Continue")
        self.assertTrue(navigation["enabled"])

    def test_visible_next_button_is_treated_as_navigation_when_role_lookup_fails(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        modal = VisibleOnlyNextModal()
        navigation = automation._find_navigation_button(modal)
        summary = automation._navigation_control_summary(modal)

        self.assertEqual(navigation["text"], "Next")
        self.assertTrue(navigation["enabled"])
        self.assertTrue(summary["Next"]["available"])
        self.assertTrue(summary["Next"]["enabled"])

    def test_upload_resume_prompt_is_not_treated_as_selected_resume(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        status = automation._resume_upload_status(UploadResumeRequiredModal())

        self.assertTrue(status["resume_upload_present"])
        self.assertFalse(status["selected_resume_present"])

    def test_workflow_trace_records_repeated_page_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            automation = LinkedInAutomation(
                Settings(openai_api_key=None, workflow_debug_dir=temporary_path)
            )
            debug_dir = temporary_path / "workflow"
            trace: list[dict] = []
            seen: dict[str, int] = {}
            page = FakeEvidencePage()
            modal = ValidationErrorModal()

            first = automation._record_workflow_trace(
                page,
                modal,
                debug_dir,
                trace,
                seen,
                1,
                "inspect",
                False,
                None,
                0,
                datetime.utcnow(),
            )
            second = automation._record_workflow_trace(
                page,
                modal,
                debug_dir,
                trace,
                seen,
                2,
                "inspect",
                False,
                None,
                0,
                datetime.utcnow(),
            )

            self.assertEqual(first["repeated_count"], 1)
            self.assertEqual(second["repeated_count"], 2)
            self.assertTrue((debug_dir / "workflow_trace.json").exists())

    def test_validation_error_is_meaningful_navigation_failure(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        page = FakeEvidencePage()
        before = automation._workflow_state(page, FakeEasyApplyModal())
        after = automation._workflow_state(page, ValidationErrorModal())

        reason = automation._navigation_failure_reason(before, after, "Next")

        self.assertIn("Validation error", reason)
        self.assertIn("acceptable document format", reason)

    def test_employment_validation_page_is_allowed_for_autofill(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        reason = automation._navigation_failure_reason(
            {"page_hash": "before", "unresolved_required_fields": []},
            {
                "page_hash": "after",
                "detected_page_type": "validation_error",
                "validation_messages": ["Please enter a valid answer"],
                "unresolved_required_fields": [
                    {"label": "Month of From"},
                    {"label": "Month of To"},
                ],
            },
            "Next",
        )
        self.assertIsNone(reason)

    def test_unchanged_modal_state_reports_no_navigation(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        page = FakeEvidencePage()
        state = automation._workflow_state(page, FakeEasyApplyModal())

        reason = automation._navigation_failure_reason(state, state, "Next")

        self.assertIsNone(reason)

    def test_step_limit_reason_uses_validation_message(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))

        reason = automation._step_limit_reason(ValidationErrorModal())

        self.assertIn("validation prevented progress", reason)
        self.assertIn("acceptable document format", reason)

    def test_required_placeholder_select_causes_needs_review(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        modal = FakeContactModal(
            [
                FakePlaceholderSelectField(
                    "Do you have data analysis experience with an E-Commerce company?"
                )
            ]
        )

        blocker = automation._detect_unanswered_custom_questions(modal)

        self.assertIn("Required questions need review", blocker)
        self.assertIn("E-Commerce", blocker)

    def test_salary_question_estimates_value_in_aggressive_mode(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data Scientist with Python, SQL and machine learning experience.",
            skills=["Python", "SQL"],
            technologies=["Python"],
            education=[],
            experience=[],
            job_titles=["Data Scientist"],
            keywords=[],
            detected_role="Data Scientist",
        )
        job = JobOpportunity(
            resume_id=1,
            title="Data Scientist",
            company="Example",
            location="India",
            job_url="https://example.com/job",
            description="Remote data scientist role.",
            easy_apply=True,
        )

        answer = automation._select_screening_answer(
            "What is your expected CTC?",
            "salary",
            {"field_type": "text", "type": "text"},
            [],
            resume,
            job,
            CandidateProfile(current_location="Mumbai, India"),
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertIsNotNone(answer)
        self.assertEqual(answer["source"], "AI Estimated Salary")
        self.assertEqual(answer["confidence"], "Estimated / Low")
        self.assertEqual(answer["currency"], "INR")
        self.assertTrue(answer["selected_answer"].isdigit())

    def test_salary_question_reuses_saved_candidate_answer_first(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="No salary listed.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(
            resume_id=1,
            title="Analyst",
            company="Example",
            location="United States",
            job_url="https://example.com/job",
        )

        answer = automation._select_screening_answer(
            "Expected salary",
            "salary",
            {"field_type": "text", "type": "text"},
            [],
            resume,
            job,
            None,
            {
                "mode": "aggressive",
                "saved_answers": [
                    {
                        "normalized_question": "expected salary",
                        "category": "salary",
                        "answer": "120000",
                        "source": "Candidate Knowledge Base",
                        "confidence": "High",
                        "currency": "USD",
                    }
                ],
            },
        )

        self.assertEqual(answer["selected_answer"], "120000")
        self.assertEqual(answer["source"], "Candidate Knowledge Base")
        self.assertFalse(answer["aggressive_default_used"])

    def test_salary_question_needs_review_in_safe_mode_without_saved_or_resume_value(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(
            resume_id=1,
            title="Data Analyst",
            company="Example",
            location="United States",
            job_url="https://example.com/job",
        )

        answer = automation._select_screening_answer(
            "Desired salary",
            "salary",
            {"field_type": "text", "type": "text"},
            [],
            resume,
            job,
            None,
            {"mode": "safe", "saved_answers": []},
        )

        self.assertIsNone(answer)

    def test_skill_year_questions_are_detected_and_normalized(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))

        self.assertEqual(
            automation._screening_question_category("How many years of work experience do you have with Microsoft Excel?"),
            "skill_years",
        )
        self.assertEqual(
            automation._screening_question_category("How many years of experience do you have with SQL?"),
            "skill_years",
        )
        self.assertEqual(
            automation._screening_question_category("Years of Python experience?"),
            "skill_years",
        )
        self.assertEqual(
            automation._extract_skill_from_experience_question("Microsoft Excel years?"),
            "Excel",
        )
        self.assertEqual(
            automation._extract_skill_from_experience_question("Python (Programming Language) experience in years?"),
            "Python",
        )
        self.assertEqual(
            automation._extract_skill_from_experience_question("How many years of work experience do you have with Databases?"),
            "Databases",
        )

    def test_skill_year_question_is_not_routed_to_employment_before_screening(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        question = "How many years of work experience do you have with Microsoft Excel?"
        category = automation._screening_question_category(question)

        self.assertEqual(category, "skill_years")
        self.assertEqual(automation._employment_field_key(question, FakeLocator()), "total_experience_years")
        self.assertTrue(automation._should_screening_handle_field(question, category))

    def test_education_and_relocation_questions_are_screening_owned(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))

        self.assertTrue(
            automation._should_screening_handle_field(
                "Have you completed a Bachelor's Degree?",
                automation._screening_question_category("Have you completed a Bachelor's Degree?"),
            )
        )
        self.assertTrue(
            automation._should_screening_handle_field(
                "Are you open to relocation?",
                automation._screening_question_category("Are you open to relocation?"),
            )
        )

    def test_graduation_year_uses_resume_education_year(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Education: Bachelor of Engineering, University of Mumbai, Graduation 2026",
            skills=[],
            technologies=[],
            education=[{"degree": "Bachelor of Engineering", "year": 2026}],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        category = automation._screening_question_category("Please provide your graduation Year for your highest completed degree.")
        answer = automation._select_screening_answer(
            "Please provide your graduation Year for your highest completed degree.",
            category,
            {"field_type": "input", "type": "text"},
            [],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(category, "graduation_year")
        self.assertEqual(answer["selected_answer"], "2026")
        self.assertEqual(answer["decision_stage"], "evidence")

    def test_graduation_year_textbox_does_not_receive_boolean_fallback(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Bachelor of Engineering, University of Mumbai.",
            skills=[],
            technologies=[],
            education=[{"degree": "Bachelor of Engineering"}],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "Graduation Year",
            automation._screening_question_category("Graduation Year"),
            {"field_type": "input", "type": "text"},
            [],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertIsNone(answer)

    def test_text_fields_do_not_get_yes_no_fallback_answers(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
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
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        for question in ("LinkedIn Profile", "Cover Letter"):
            answer = automation._select_screening_answer(
                question,
                automation._screening_question_category(question),
                {"field_type": "textarea" if question == "Cover Letter" else "input", "type": "textarea" if question == "Cover Letter" else "text"},
                [],
                resume,
                job,
                None,
                {"mode": "aggressive", "saved_answers": []},
            )
            self.assertIsNone(answer)

    def test_radio_writer_selects_option_matching_answer(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField("Are you open to relocation?", ["Yes", "No"])

        result = automation._fill_screening_field(field, {"field_type": "radio", "type": "radio"}, "No", ["Yes", "No"])

        self.assertEqual(field.selected_option, "No")
        self.assertEqual(result["final_write_status"], "success")
        self.assertEqual(result["click_strategy_used"], "label_click")

    def test_radio_writer_records_parent_container_fallback(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField(
            "Are you willing to undergo a background check?",
            ["Yes", "No"],
            strategy="parent_container_click",
        )

        result = automation._fill_screening_field(field, {"field_type": "radio", "type": "radio"}, "Yes", ["Yes", "No"])

        self.assertEqual(field.selected_option, "Yes")
        self.assertEqual(result["click_strategy_used"], "parent_container_click")

    def test_radio_writer_records_hidden_input_fallback(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField(
            "Are you comfortable commuting to this job's location?",
            ["Yes", "No"],
            strategy="native_checked_change",
        )

        result = automation._fill_screening_field(field, {"field_type": "radio", "type": "radio"}, "Yes", ["Yes", "No"])

        self.assertEqual(field.selected_option, "Yes")
        self.assertEqual(result["click_strategy_used"], "native_checked_change")

    def test_radio_writer_dumps_duplicate_hidden_radio_inputs(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        diagnostics = {
            "question_text": "Are you legally authorized to work in the United States?",
            "group_outer_html": "<fieldset><input type='radio' hidden><label>Yes</label><input type='radio'><label>No</label></fieldset>",
            "inputs_before": [
                {"id": "hidden-yes", "name": "auth-1", "value": "Yes", "label": "Yes", "checked": False, "visible": False},
                {"id": "visible-yes", "name": "auth-1", "value": "Yes", "label": "Yes", "checked": False, "visible": True},
                {"id": "visible-no", "name": "auth-1", "value": "No", "label": "No", "checked": False, "visible": True},
            ],
            "inputs_after": [
                {"id": "hidden-yes", "name": "auth-1", "value": "Yes", "label": "Yes", "checked": False, "visible": False},
                {"id": "visible-yes", "name": "auth-1", "value": "Yes", "label": "Yes", "checked": True, "visible": True},
                {"id": "visible-no", "name": "auth-1", "value": "No", "label": "No", "checked": False, "visible": True},
            ],
            "active_element_after": {"id": "visible-yes"},
        }
        field = FakeScreeningRadioField(
            "Are you legally authorized to work in the United States?",
            ["Yes", "No"],
            group_diagnostics=diagnostics,
        )

        result = automation._fill_screening_field(
            field,
            {"field_type": "radio", "type": "radio"},
            "Yes",
            ["Yes", "No"],
            question="Are you legally authorized to work in the United States?",
            normalized_field="us_work_authorization",
        )

        self.assertEqual(result["final_write_status"], "success")
        self.assertEqual(result["group_diagnostics"]["inputs_before"][0]["id"], "hidden-yes")
        self.assertIn("fieldset", result["group_diagnostics"]["group_outer_html"])

    def test_radio_writer_scopes_repeated_yes_no_groups_to_current_question(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField(
            "Will you now, or in the future, require sponsorship for employment visa status?",
            ["Yes", "No"],
            expected_question="Will you now, or in the future, require sponsorship for employment visa status?",
        )

        result = automation._fill_screening_field(
            field,
            {"field_type": "radio", "type": "radio"},
            "No",
            ["Yes", "No"],
            question="Will you now, or in the future, require sponsorship for employment visa status?",
            normalized_field="visa_sponsorship",
        )

        self.assertEqual(field.selected_option, "No")
        self.assertEqual(result["question_label"], "Will you now, or in the future, require sponsorship for employment visa status?")
        self.assertEqual(field.last_payload["question"], "Will you now, or in the future, require sponsorship for employment visa status?")

    def test_radio_question_text_prefers_group_question_over_yes_option_label(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField(
            "Are you willing to undergo a background check?",
            ["Yes", "No"],
        )

        question = automation._screening_question_text(field, 0)

        self.assertEqual(question, "Are you willing to undergo a background check?")

    def test_legal_radio_question_without_saved_answer_needs_review(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
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
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")
        question = "Will you now, or in the future, require sponsorship for employment visa status?"

        answer = automation._select_screening_answer(
            question,
            automation._screening_question_category(question),
            {"field_type": "radio", "type": "radio"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertIsNone(answer)

    def test_radio_writer_treats_visual_click_without_validation_clear_as_failed(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField(
            "Have you completed the following level of education: Associate's Degree?",
            ["Yes", "No"],
            accepted=False,
            validation_after="Please make a selection",
        )

        with self.assertRaisesRegex(RuntimeError, "did not accept"):
            automation._fill_screening_field(field, {"field_type": "radio", "type": "radio"}, "Yes", ["Yes", "No"])

        self.assertIsNone(field.selected_option)

    def test_radio_writer_recovery_clears_validation_after_visual_check(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField(
            "Have you completed the following level of education: Bachelor's Degree?",
            ["Yes", "No"],
            accepted=False,
            validation_after="Please make a selection",
            recovery_succeeds=True,
        )

        result = automation._fill_screening_field(
            field,
            {"field_type": "radio", "type": "radio"},
            "Yes",
            ["Yes", "No"],
            question="Have you completed the following level of education: Bachelor's Degree?",
            normalized_field="Education Requirement",
        )

        self.assertEqual(result["final_write_status"], "success")
        self.assertEqual(result["click_strategy_used"], "post_selection_validation_recovery")
        self.assertEqual(result["validation_message_after"], "")
        self.assertTrue(any(attempt["strategy"] == "post_selection_validation_recovery" for attempt in result["attempts"]))

    def test_radio_writer_failed_recovery_remains_write_failed(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField(
            "Have you completed the following level of education: Bachelor's Degree?",
            ["Yes", "No"],
            accepted=False,
            validation_after="Please make a selection",
            recovery_succeeds=False,
        )

        with self.assertRaisesRegex(RuntimeError, "did not accept"):
            automation._fill_screening_field(
                field,
                {"field_type": "radio", "type": "radio"},
                "Yes",
                ["Yes", "No"],
            )

        self.assertIsNone(field.selected_option)

    def test_radio_writer_diagnostics_include_provider_and_selected_markup(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField("Are you open to relocation?", ["Yes", "No"])

        result = automation._fill_screening_field(
            field,
            {"field_type": "radio", "type": "radio"},
            "Yes",
            ["Yes", "No"],
        )

        self.assertEqual(result["group_diagnostics"]["provider_name"], "linkedin_native")
        self.assertIn("selected_input_outer_html", result["group_diagnostics"])
        self.assertIn("exact_label_clicked", result["group_diagnostics"])

    def test_numeric_text_fields_are_unaffected_by_radio_writer_recovery(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeContactField("How many years of work experience do you have with SQL?", field_type="number")

        result = automation._fill_screening_field(
            field,
            {"field_type": "number", "type": "number"},
            "1",
            [],
        )

        self.assertIsNone(result)
        self.assertEqual(field.filled_values, ["1"])

    def test_radio_writer_diagnostics_are_written(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = FakeScreeningRadioField("Are you open to relocation?", ["Yes", "No"])
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="Remote", job_url="https://example.com")

        with tempfile.TemporaryDirectory() as temp_dir:
            debug_dir = Path(temp_dir)
            automation._fill_screening_field(
                field,
                {"field_type": "radio", "type": "radio"},
                "Yes",
                ["Yes", "No"],
                job=job,
                question="Are you open to relocation?",
                normalized_field="relocation",
                radio_debug_dir=debug_dir,
            )

            payload = json.loads((debug_dir / "radio_writer_events.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["events"][0]["question_label"], "Are you open to relocation?")
        self.assertEqual(payload["events"][0]["target_answer"], "Yes")
        self.assertEqual(payload["events"][0]["final_write_status"], "success")
        self.assertIn("group_outer_html", payload["events"][0]["group_diagnostics"])
        self.assertIn("inputs_before", payload["events"][0]["group_diagnostics"])

    def test_degree_dropdown_prefers_resume_degree_option(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Bachelor of Engineering in Computer Science.",
            skills=[],
            technologies=[],
            education=[{"degree": "Bachelor of Engineering"}],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "Degree",
            automation._screening_question_category("Degree"),
            {"field_type": "select", "type": "select-one"},
            ["High School", "Bachelor's Degree", "Master's Degree"],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(answer["selected_answer"], "Bachelor's Degree")
        self.assertEqual(answer["decision_stage"], "evidence")

    def test_skill_found_in_resume_skills_aggressive_enters_one(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Skills: Excel, SQL, Python",
            skills=["Excel", "SQL", "Python"],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "How many years of work experience do you have with Microsoft Excel?",
            "skill_years",
            {"field_type": "input", "type": "number"},
            [],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(answer["selected_answer"], "1")
        self.assertEqual(answer["evidence"]["normalized_skill"], "Excel")
        self.assertTrue(answer["aggressive_default_used"])

    def test_databases_years_question_reuses_sql_database_evidence(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Skills: SQL, PostgreSQL, relational database design",
            skills=["SQL"],
            technologies=["PostgreSQL"],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "How many years of work experience do you have with Databases?",
            "skill_years",
            {"field_type": "input", "type": "number"},
            [],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(answer["selected_answer"], "1")
        self.assertEqual(answer["evidence"]["normalized_skill"], "Databases")

    def test_skill_found_in_experience_dates_calculates_years(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data Analyst 2021 to 2024. Built SQL dashboards and data models.",
            skills=["SQL"],
            technologies=[],
            education=[],
            experience=[{"text": "Data Analyst 2021 to 2024. Built SQL dashboards."}],
            job_titles=[],
            keywords=[],
        )

        estimate = automation._estimate_skill_years("SQL", resume, None)

        self.assertEqual(estimate["years"], 3)
        self.assertEqual(estimate["confidence"], "High")

    def test_skill_missing_aggressive_enters_zero(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Reporting analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "How many years of experience do you have with Tableau?",
            "skill_years",
            {"field_type": "input", "type": "number"},
            [],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(answer["selected_answer"], "0")
        self.assertTrue(answer["aggressive_default_used"])

    def test_skill_missing_fills_zero_years(self) -> None:
        # Numeric skill-years fields must always receive a number so LinkedIn's
        # "whole number 0-99" validation passes. With no resume evidence the honest
        # AI-generated value is 0 (never a yes/no string).
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Reporting analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="India", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "How many years of experience do you have with Tableau?",
            "skill_years",
            {"field_type": "input", "type": "number"},
            [],
            resume,
            job,
            None,
            {"mode": "safe", "saved_answers": []},
        )

        self.assertIsNotNone(answer)
        self.assertEqual(answer["selected_answer"], "0")
        self.assertTrue(str(answer["selected_answer"]).lstrip("-").isdigit())

    def test_saved_skill_year_answer_is_reused(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        saved = {
            "normalized_question": "sql years experience",
            "category": "skill_years",
            "answer": "2",
            "source": "Candidate Knowledge Base",
            "confidence": "High",
            "evidence": {"normalized_skill": "SQL"},
        }

        match = automation._matching_saved_screening_answer(
            "How many years of work experience do you have with SQL?",
            "skill_years",
            [saved],
        )

        self.assertEqual(match["answer"], "2")

    def test_skill_experience_estimates_are_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            automation = LinkedInAutomation(Settings(openai_api_key=None))
            debug_dir = Path(temporary_directory)
            event = {
                "question_text": "How many years of experience do you have with Python?",
                "category": "skill_years",
                "field_type": "input",
                "options": [],
                "selected_answer": "1",
                "source": "Resume Skills",
                "confidence": "Estimated",
                "reason": "Skill appeared in skills section; used conservative 1 year estimate.",
                "aggressive_default_used": True,
                "mode": "Aggressive",
                "evidence": {
                    "normalized_skill": "Python",
                    "evidence_used": "Skill appeared in resume skills or technologies.",
                },
            }

            automation._save_screening_debug(debug_dir, [], [event])

            questions = __import__("json").loads((debug_dir / "skill_experience_questions.json").read_text())
            estimates = __import__("json").loads((debug_dir / "skill_experience_estimates.json").read_text())
            trace = __import__("json").loads((debug_dir / "field_decision_trace.json").read_text())
            self.assertEqual(questions[0]["normalized_skill"], "Python")
            self.assertEqual(estimates[0]["selected_years"], "1")
            self.assertEqual(trace[0]["original_label"], "How many years of experience do you have with Python?")
            self.assertEqual(trace[0]["selected_value"], "1")

    def test_unknown_yes_no_selects_no_in_aggressive_mode(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst with reporting experience.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "Do you have E-Commerce experience?",
            "yes_no",
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(answer["selected_answer"], "No")
        self.assertTrue(answer["aggressive_default_used"])

    def test_work_authorization_never_defaults_yes_or_no_without_saved_answer(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst with SQL experience.",
            skills=["SQL"],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")
        question = "Are you a US Citizen or Green Card holder?"

        answer = automation._select_screening_answer(
            question,
            automation._screening_question_category(question),
            {"field_type": "radio", "type": "radio"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertIsNone(answer)

    def test_work_authorization_uses_saved_answer_first(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")
        question = "Are you legally authorized to work in the United States?"

        answer = automation._select_screening_answer(
            question,
            automation._screening_question_category(question),
            {"field_type": "radio", "type": "radio"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {
                "mode": "aggressive",
                "saved_answers": [
                    {
                        "normalized_question": automation._normalize_screening_question(question),
                        "answer": "Yes",
                        "source": "Candidate Knowledge Base",
                        "confidence": "High",
                    }
                ],
            },
        )

        self.assertEqual(answer["selected_answer"], "Yes")
        self.assertEqual(answer["source"], "Candidate Knowledge Base")

    def test_visa_sponsorship_and_background_check_require_saved_answers(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst with reporting experience.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")
        for question in (
            "Will you now or in the future require sponsorship?",
            "Are you willing to undergo a background check?",
        ):
            answer = automation._select_screening_answer(
                question,
                automation._screening_question_category(question),
                {"field_type": "radio", "type": "radio"},
                ["Yes", "No"],
                resume,
                job,
                None,
                {"mode": "aggressive", "saved_answers": []},
            )
            self.assertIsNone(answer)

    def test_preference_questions_are_answered_conservatively(self) -> None:
        # Work-preference (relocation / commute / hybrid) is non-legal and is
        # auto-answered with the application-friendly conservative default (Yes).
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst with reporting experience.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="Boston, MA", job_url="https://example.com")
        for question in (
            "Are you open to relocation?",
            "Are you comfortable commuting to this job's location?",
            "Are you comfortable working in a hybrid setting?",
        ):
            answer = automation._select_screening_answer(
                question,
                automation._screening_question_category(question),
                {"field_type": "radio", "type": "radio"},
                ["Yes", "No"],
                resume,
                job,
                CandidateProfile(current_location="Boston, MA"),
                {"mode": "aggressive", "saved_answers": []},
            )
            self.assertIsNotNone(answer, question)
            self.assertEqual(answer["selected_answer"], "Yes", question)

    def test_driver_license_and_clearance_use_explicit_candidate_profile_evidence(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")
        candidate_profile = CandidateProfile(certifications=["Driver's License", "Active security clearance"])

        for question in (
            "Do you have a valid driver's license?",
            "Do you hold an active security clearance?",
        ):
            answer = automation._select_screening_answer(
                question,
                automation._screening_question_category(question),
                {"field_type": "radio", "type": "radio"},
                ["Yes", "No"],
                resume,
                job,
                candidate_profile,
                {"mode": "aggressive", "saved_answers": []},
            )
            self.assertEqual(answer["selected_answer"], "Yes")
            self.assertEqual(answer["source"], "Candidate Profile")

    def test_numeric_field_never_receives_yes_no_answer(self) -> None:
        # Regression for the live bug where "Yes" was typed into a "years of
        # experience" field (label mis-resolved to a neighbouring yes/no question).
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt", stored_path="resume.txt",
            raw_text="Data analyst with 3 years of SQL experience.",
            skills=["SQL"], technologies=[], education=[], experience=[], job_titles=[], keywords=[],
        )
        context = {"mode": "safe", "saved_answers": []}

        # A yes/no answer mis-routed into a numeric field becomes a whole number.
        coerced = automation._typesafe_answer_value(
            "Yes",
            {"field_type": "text", "type": "text", "numeric": True},
            [],
            "How many years of work experience do you have with Python (Programming Language)?",
            resume, None, context,
        )
        self.assertTrue(re.fullmatch(r"-?\d+", str(coerced)), coerced)

        # A skill with evidence yields its estimated number, not "Yes".
        sql_years = automation._typesafe_answer_value(
            "Yes",
            {"field_type": "text", "type": "text", "numeric": True},
            [],
            "How many years of work experience do you have with SQL?",
            resume, None, context,
        )
        self.assertTrue(re.fullmatch(r"-?\d+", str(sql_years)))

        # A yes/no answer for a plain free-text input is dropped (left blank).
        self.assertIsNone(
            automation._typesafe_answer_value(
                "No", {"field_type": "text", "type": "text", "numeric": False}, [],
                "Tell us about yourself", resume, None, context,
            )
        )

        # Radio/choice answers are never altered.
        self.assertEqual(
            automation._typesafe_answer_value(
                "Yes", {"field_type": "radio", "type": "radio"}, ["Yes", "No"],
                "Are you comfortable commuting to this job's location?", resume, None, context,
            ),
            "Yes",
        )

    def test_driver_license_does_not_fabricate_yes_without_evidence(self) -> None:
        # A driver's license (certification, non-legal) is answered honestly: with
        # no profile/saved/resume evidence the AI-generated answer is "No" (it never
        # fabricates a "Yes" for a credential the candidate does not have).
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")
        question = "Do you have a valid driver's license?"

        answer = automation._select_screening_answer(
            question,
            automation._screening_question_category(question),
            {"field_type": "radio", "type": "radio"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertIsNotNone(answer)
        self.assertEqual(answer["selected_answer"], "No")

    def test_employment_history_database_save(self) -> None:
        candidate = CandidateProfile(is_default=True)
        resume = ResumeProfile(
            filename="employment.txt",
            stored_path="employment.txt",
            raw_text="Experience",
            skills=[],
            experience=[],
            education=[],
            job_titles=[],
            keywords=[],
            technologies=[],
            domain_expertise=[],
        )
        self.db.add(candidate)
        self.db.add(resume)
        self.db.commit()
        self.db.refresh(candidate)
        self.db.refresh(resume)

        save_extracted_employment_history(
            self.db,
            candidate,
            resume,
            [
                {
                    "display_order": 0,
                    "job_title": "Data Analyst Intern",
                    "company_name": "Ven Analytics",
                    "city": "Pune",
                    "start_month": "January",
                    "start_year": 2025,
                    "end_month": "April",
                    "end_year": 2025,
                    "currently_work_here": False,
                    "description": "Built dashboards.",
                    "skills_used": ["sql"],
                    "technologies_used": ["Excel"],
                    "duration_months": 4,
                    "confidence": 86,
                    "source": "Resume Experience",
                }
            ],
        )

        records = list(
            self.db.scalars(
                select(CandidateEmploymentHistory).where(
                    CandidateEmploymentHistory.resume_id == resume.id
                )
            ).all()
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].company_name, "Ven Analytics")

    def test_employment_field_mapping_fills_dates_and_description(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        fields = [
            EmploymentField("Company"),
            EmploymentField("Job Title"),
            EmploymentField("Location"),
            EmploymentField("Month of From", tag="select", field_type="select", options=["Select", "January", "May"]),
            EmploymentField("Year of From", tag="select", field_type="select", options=["Select", "2022", "2023"]),
            EmploymentField("Month of To", tag="select", field_type="select", options=["Select", "June", "December"]),
            EmploymentField("Year of To", tag="select", field_type="select", options=["Select", "2023", "2024"]),
            EmploymentField("Description", tag="textarea", field_type="textarea"),
        ]
        modal = EmploymentHistoryModal(fields)
        events: list[dict] = []

        missing = automation._fill_employment_history_fields(
            modal,
            [
                {
                    "company_name": "Claroda",
                    "job_title": "Data Engineer",
                    "city": "Pune",
                    "state": "Maharashtra",
                    "country": "India",
                    "start_month": "May",
                    "start_year": 2022,
                    "end_month": "June",
                    "end_year": 2023,
                    "currently_work_here": False,
                    "description": "Built SQL ETL pipelines.",
                    "confidence": 86,
                }
            ],
            CandidateProfile(),
            None,
            "safe",
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertEqual(fields[0]._attributes["value"], "Claroda")
        self.assertEqual(fields[3]._attributes["value"], "May")
        self.assertEqual(fields[5]._attributes["value"], "June")
        self.assertEqual(fields[7]._attributes["value"], "Built SQL ETL pipelines.")

    def test_employment_total_experience_dropdowns_are_filled_from_durations(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        years = EmploymentField(
            "Please select your total years of professional experience:",
            tag="select",
            field_type="select",
            options=["Select an option", "0", "1", "2", "3", "4", "5"],
        )
        months = EmploymentField(
            "Please select your total additional months of experience:",
            tag="select",
            field_type="select",
            options=["Select an option", "0", "1", "2", "3", "4", "5", "6"],
        )
        modal = EmploymentHistoryModal([years, months])
        events: list[dict] = []

        missing = automation._fill_employment_history_fields(
            modal,
            [
                {"duration_months": 30, "confidence": 86},
                {"duration_months": 7, "confidence": 86},
                {"duration_months": 4, "confidence": 86},
                {"duration_months": 7, "confidence": 86},
            ],
            None,
            None,
            "safe",
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertEqual(years._attributes["value"], "4")
        self.assertEqual(months._attributes["value"], "0")

    def test_employment_year_select_is_detected_from_options_when_label_says_month(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = EmploymentField(
            "Month of From",
            tag="select",
            field_type="select",
            options=["Year", "2024", "2023", "2022"],
        )
        key = automation._employment_field_key("Month of From", field)
        self.assertEqual(key, "start_year")

    def test_employment_edit_form_prefers_save_before_next(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        modal = EmploymentEditModal([EmploymentField("Company", value="Claroda")])
        navigation = automation._find_navigation_button(modal)
        self.assertEqual(navigation["text"], "Save")

    def test_current_employer_checkbox_leaves_end_dates_blank(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        current_field = EmploymentField("I currently work here", field_type="checkbox", required=False)
        end_month = EmploymentField("Month of To", tag="select", field_type="select", required=False, options=["Select", "January"])
        modal = EmploymentHistoryModal([current_field, end_month])
        events: list[dict] = []

        missing = automation._fill_employment_history_fields(
            modal,
            [{"currently_work_here": True, "confidence": 86}],
            None,
            None,
            "safe",
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertTrue(current_field.clicked)
        self.assertEqual(end_month._attributes.get("value", ""), "")

    def test_aggressive_mode_estimates_missing_month_from_year(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = EmploymentField("Month of From", tag="select", field_type="select", options=["Select", "January"])
        modal = EmploymentHistoryModal([field])
        events: list[dict] = []

        missing = automation._fill_employment_history_fields(
            modal,
            [{"start_year": 2022, "confidence": 70}],
            None,
            None,
            "aggressive",
            None,
            events,
        )

        self.assertEqual(missing, [])
        self.assertEqual(field._attributes["value"], "January")
        self.assertTrue(events[0]["estimated"])

    def test_safe_mode_does_not_estimate_missing_month(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = EmploymentField("Month of From", tag="select", field_type="select", options=["Select", "January"])
        modal = EmploymentHistoryModal([field])
        events: list[dict] = []

        missing = automation._fill_employment_history_fields(
            modal,
            [{"start_year": 2022, "confidence": 70}],
            None,
            None,
            "safe",
            None,
            events,
        )

        self.assertEqual(missing, ["Month of From"])
        self.assertEqual(field._attributes.get("value", ""), "")

    def test_employment_debug_files_are_written(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        field = EmploymentField("Company")
        modal = EmploymentHistoryModal([field])
        with tempfile.TemporaryDirectory() as temporary_directory:
            debug_dir = Path(temporary_directory)
            automation._fill_employment_history_fields(
                modal,
                [{"company_name": "Claroda", "confidence": 86}],
                None,
                None,
                "safe",
                debug_dir,
                [],
            )
            self.assertTrue((debug_dir / "employment_records.json").exists())
            self.assertTrue((debug_dir / "employment_mapping.json").exists())
            self.assertTrue((debug_dir / "employment_fields_filled.json").exists())
            self.assertTrue((debug_dir / "employment_validation.json").exists())

    def test_unknown_nonlegal_yes_no_is_ai_answered_in_safe_mode(self) -> None:
        # Non-legal unknown question is auto-answered (AI-generated honest "No"),
        # not left blank, per the auto-answer product decision.
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Data analyst.",
            skills=[],
            technologies=[],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Analyst", company="Example", location="United States", job_url="https://example.com")

        answer = automation._select_screening_answer(
            "Do you have E-Commerce experience?",
            "yes_no",
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {"mode": "safe", "saved_answers": []},
        )

        self.assertIsNotNone(answer)
        self.assertEqual(answer["selected_answer"], "No")

    def test_skill_question_uses_resume_evidence(self) -> None:
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Python and SQL developer.",
            skills=["SQL"],
            technologies=["Python"],
            education=[],
            experience=[],
            job_titles=[],
            keywords=[],
        )
        job = JobOpportunity(resume_id=1, title="Engineer", company="Example", location="United States", job_url="https://example.com")

        yes_answer = automation._select_screening_answer(
            "Do you have SQL experience?",
            "skill",
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )
        no_answer = automation._select_screening_answer(
            "Do you have Tableau experience?",
            "skill",
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            resume,
            job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(yes_answer["selected_answer"], "Yes")
        self.assertEqual(no_answer["selected_answer"], "No")

    def test_form_navigation_step_file_records_stop_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            automation = LinkedInAutomation(Settings(openai_api_key=None))
            debug_dir = Path(temporary_directory) / "form_debug"
            steps: list[dict] = []
            automation._record_form_navigation_step(
                FakeEvidencePage(),
                ContinueEasyApplyModal(),
                debug_dir,
                steps,
                1,
                "none",
                "stopped",
                "Button not found",
            )

            path = debug_dir / "form_navigation_steps.json"
            self.assertTrue(path.exists())
            payload = __import__("json").loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["step_number"], 1)
            self.assertEqual(payload[0]["reason_for_stop"], "Button not found")
            self.assertIn("visible_buttons", payload[0])


if __name__ == "__main__":
    unittest.main()
