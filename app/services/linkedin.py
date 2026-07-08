import json
import logging
import re
import hashlib
import mimetypes
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings
from app.models import ApplicationStatus, CandidateProfile, JobOpportunity, ResumeProfile
from app.services.reasoning import AIReasoningEngine
from app.services.screening_engine import ScreeningAnswerEngine, ScreeningQuestion

logger = logging.getLogger(__name__)


@dataclass
class AutomationResult:
    status: ApplicationStatus
    message: str | None = None
    log: list[dict] = field(default_factory=list)
    current_step: str | None = None
    evidence_path: str | None = None
    contact_fields_detected: list[dict] = field(default_factory=list)
    contact_fields_filled: list[dict] = field(default_factory=list)
    missing_contact_fields: list[str] = field(default_factory=list)
    screening_answers: list[dict] = field(default_factory=list)
    resume_upload_diagnostics: dict = field(default_factory=dict)


class LinkedInAutomation:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.reasoning = AIReasoningEngine(settings)
        # Portal-agnostic screening decision engine (shared with Dice/other portals).
        self.screening = ScreeningAnswerEngine(settings, reasoning=self.reasoning)

    def has_authenticated_session(self) -> bool:
        if not self.settings.linkedin_storage_state.exists():
            return False
        try:
            state = json.loads(self.settings.linkedin_storage_state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return any(
            cookie.get("name") == "li_at"
            and cookie.get("value")
            and cookie.get("domain", "").endswith("linkedin.com")
            for cookie in state.get("cookies", [])
        )

    def disconnect_account(self) -> None:
        self.settings.linkedin_storage_state.unlink(missing_ok=True)

    def connect_account(self) -> None:
        if self.has_authenticated_session():
            return

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=False,
                slow_mo=self.settings.linkedin_slow_mo_ms,
            )
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            try:
                for _ in range(360):
                    cookies = context.cookies()
                    if page.url.startswith("https://www.linkedin.com/feed") or any(
                        cookie.get("name") == "li_at" and cookie.get("value") for cookie in cookies
                    ):
                        context.storage_state(path=str(self.settings.linkedin_storage_state))
                        return
                    page.wait_for_timeout(500)
                raise PlaywrightTimeoutError("LinkedIn login was not completed within 180 seconds.")
            except PlaywrightError:
                if self.has_authenticated_session():
                    return
                raise
            finally:
                browser.close()

    def extract_logged_in_profile(self) -> dict:
        if not self.has_authenticated_session():
            return {"fields": {}, "professional_links": {}}
        fields: dict[str, dict] = {}

        def add(name: str, value: str | None, confidence: float) -> None:
            value = (value or "").strip()
            if not value:
                return
            fields[name] = {
                "value": value,
                "source": "LinkedIn Profile",
                "confidence": confidence,
            }

        with self._browser_context() as context:
            page = context.new_page()
            page.goto("https://www.linkedin.com/in/me/", wait_until="domcontentloaded")
            self._wait_for_job_page(page)
            page.wait_for_timeout(1500)
            profile_url = page.url.split("?")[0].rstrip("/")
            add("linkedin_url", profile_url, 82)
            name = self._first_text(
                page,
                [
                    "main h1",
                    ".pv-text-details__left-panel h1",
                    ".text-heading-xlarge",
                ],
            )
            if name:
                parts = name.split()
                add("first_name", parts[0], 72)
                if len(parts) > 1:
                    add("last_name", parts[-1], 72)
            headline = self._first_text(
                page,
                [
                    ".text-body-medium.break-words",
                    ".pv-text-details__left-panel .text-body-medium",
                ],
            )
            add("professional_headline", headline, 70)
            add("current_job_title", headline, 52)
            location = self._first_text(
                page,
                [
                    ".text-body-small.inline.t-black--light.break-words",
                    ".pv-text-details__left-panel .text-body-small",
                ],
            )
            add("current_location", location, 68)
            page_text = self._first_text(page, ["main"])
        return {
            "fields": fields,
            "professional_links": {},
            "profile_text": page_text[:4000] if "page_text" in locals() else "",
        }

    def search_jobs(
        self,
        resume: ResumeProfile,
        location: str,
        keywords: str | None,
        limit: int,
        collect_card_diagnostics: bool = True,
    ) -> list[dict]:
        query = (
            (keywords or "").strip()
            or resume.detected_role
            or " ".join((resume.job_titles or resume.skills or resume.keywords)[:6])
            or "Software Engineer"
        )
        url = (
            "https://www.linkedin.com/jobs/search/"
            f"?keywords={quote_plus(query)}&location={quote_plus(location)}&f_AL=true"
        )
        jobs: list[dict] = []
        debug_timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        with self._browser_context() as context:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            card_selector = (
                "li[data-occludable-job-id], .scaffold-layout__list-item, "
                "ul.jobs-search__results-list li, .jobs-search-results__list-item"
            )
            cards = page.locator(card_selector)
            count = min(cards.count(), limit)
            job_urls: list[str] = []
            for index in range(count):
                try:
                    cards = page.locator(card_selector)
                    if index >= cards.count():
                        break
                    card = cards.nth(index)
                    if collect_card_diagnostics:
                        card.scroll_into_view_if_needed()
                    links = card.locator("a[href*='/jobs/view/']")
                    if links.count() == 0:
                        continue
                    href = links.first.get_attribute("href")
                    if href:
                        job_url = urljoin("https://www.linkedin.com", href).split("?")[0]
                        if collect_card_diagnostics:
                            linkedin_job_id = self._linkedin_job_id(job_url, card)
                            debug_dir = self._create_easy_apply_debug_dir(
                                job_id=linkedin_job_id,
                                timestamp=debug_timestamp,
                            )
                            self._capture_debug_screenshot(page, debug_dir, "01_job_card_click")
                            click_diagnostic = self._inspect_job_card_click(
                                context,
                                page,
                                card,
                                links.first,
                                job_url,
                                linkedin_job_id,
                                debug_dir,
                            )
                            self._merge_diagnostic_file(debug_dir, click_diagnostic)
                        job_urls.append(job_url)
                        if page.url != url:
                            page.goto(url, wait_until="domcontentloaded")
                            page.wait_for_timeout(1500)
                except Exception as exc:
                    logger.warning("Skipping LinkedIn result link %s: %s", index, exc)

            for job_url in list(dict.fromkeys(job_urls))[:limit]:
                try:
                    page.goto(job_url, wait_until="domcontentloaded")
                    readiness = {
                        "domcontentloaded": True,
                        "networkidle": False,
                        "main_content": False,
                        "final_url": page.url,
                    }
                    try:
                        page.locator("main, .jobs-details, .job-view-layout").first.wait_for(
                            state="visible",
                            timeout=3000,
                        )
                        readiness["main_content"] = True
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(500)
                    readiness["final_url"] = page.url
                    linkedin_job_id = self._linkedin_job_id(job_url)
                    debug_dir = None
                    if collect_card_diagnostics:
                        debug_dir = self._create_easy_apply_debug_dir(
                            job_id=linkedin_job_id,
                            timestamp=debug_timestamp,
                        )
                        self._capture_debug_screenshot(page, debug_dir, "02_job_details_page_loaded")
                        self._capture_debug_screenshot(page, debug_dir, "03_before_easy_apply_detection")
                    title = self._first_text(page, [".jobs-unified-top-card__job-title", ".top-card-layout__title", "h1"])
                    company = self._first_text(
                        page,
                        [
                            ".jobs-unified-top-card__company-name",
                            ".job-details-jobs-unified-top-card__company-name",
                            ".topcard__org-name-link",
                            ".topcard__flavor",
                        ],
                    )
                    place = self._first_text(
                        page,
                        [
                            ".jobs-unified-top-card__bullet",
                            ".job-details-jobs-unified-top-card__primary-description",
                            ".topcard__flavor--bullet",
                        ],
                    )
                    description = self._first_text(
                        page,
                        ["#job-details", ".jobs-description-content__text", ".show-more-less-html__markup"],
                    )
                    main_text = self._first_text(page, ["main"])
                    page_title_parts = [part.strip() for part in page.title().split("|")]
                    if not title and page_title_parts:
                        title = page_title_parts[0]
                    if not company and len(page_title_parts) > 1:
                        company = page_title_parts[1]
                    if not place and main_text:
                        lines = [line.strip() for line in main_text.splitlines() if line.strip()]
                        try:
                            title_index = lines.index(title)
                            place = lines[title_index + 1] if title_index + 1 < len(lines) else ""
                        except ValueError:
                            place = ""
                    if not description:
                        description = main_text
                    search_job = JobOpportunity(
                        id=0,
                        resume_id=resume.id,
                        title=title or "Unknown LinkedIn job",
                        company=company or "Unknown company",
                        location=place,
                        job_url=job_url,
                        description=description or "",
                        easy_apply=False,
                        match_score=0,
                    )
                    detected_locator, search_diagnostics = self._detect_easy_apply(
                        page,
                        search_job,
                        readiness,
                    )
                    easy_apply = detected_locator is not None
                    search_diagnostics.update(
                        {
                            "linkedin_job_id": linkedin_job_id,
                            "phase": "search",
                            "search_phase_easy_apply_result": easy_apply,
                            "application_phase_easy_apply_result": None,
                            "EASY_APPLY_MISMATCH": False,
                            "failure_reason": None if easy_apply else search_diagnostics["reason"],
                            "navigation_events": [
                                self._navigation_state(page, "search_job_details_navigation")
                            ],
                        }
                    )
                    if debug_dir:
                        self._capture_debug_screenshot(page, debug_dir, "04_after_easy_apply_detection")
                        self._merge_diagnostic_file(debug_dir, search_diagnostics)
                    if title and company:
                        jobs.append(
                            {
                                "title": title,
                                "company": company,
                                "location": place,
                                "description": description,
                                "job_url": job_url,
                                "easy_apply": easy_apply,
                            }
                        )
                except Exception as exc:
                    logger.warning("Skipping LinkedIn job %s: %s", job_url, exc)
        return jobs

    def search_jobs_many(
        self,
        resume: ResumeProfile,
        variations,
        limit_per_query: int,
        collect_card_diagnostics: bool = False,
    ) -> list[tuple[object, list[dict]]]:
        debug_timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        results: list[tuple[object, list[dict]]] = []
        with self._browser_context() as context:
            page = context.new_page()
            for variation in variations:
                results.append(
                    (
                        variation,
                        self._search_jobs_on_page(
                            context,
                            page,
                            resume,
                            getattr(variation, "location", ""),
                            getattr(variation, "query", None),
                            limit_per_query,
                            collect_card_diagnostics,
                            debug_timestamp,
                        ),
                    )
                )
        return results

    def _search_jobs_on_page(
        self,
        context: BrowserContext,
        page: Page,
        resume: ResumeProfile,
        location: str,
        keywords: str | None,
        limit: int,
        collect_card_diagnostics: bool,
        debug_timestamp: str,
    ) -> list[dict]:
        query = (
            (keywords or "").strip()
            or resume.detected_role
            or " ".join((resume.job_titles or resume.skills or resume.keywords)[:6])
            or "Software Engineer"
        )
        url = (
            "https://www.linkedin.com/jobs/search/"
            f"?keywords={quote_plus(query)}&location={quote_plus(location)}&f_AL=true"
        )
        jobs: list[dict] = []
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.locator(
                "li[data-occludable-job-id], .scaffold-layout__list-item, "
                "ul.jobs-search__results-list li, .jobs-search-results__list-item"
            ).first.wait_for(state="visible", timeout=3500)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(500)
        card_selector = (
            "li[data-occludable-job-id], .scaffold-layout__list-item, "
            "ul.jobs-search__results-list li, .jobs-search-results__list-item"
        )
        cards = page.locator(card_selector)
        count = min(cards.count(), limit)
        job_urls: list[str] = []
        for index in range(count):
            try:
                cards = page.locator(card_selector)
                if index >= cards.count():
                    break
                card = cards.nth(index)
                if collect_card_diagnostics:
                    card.scroll_into_view_if_needed()
                links = card.locator("a[href*='/jobs/view/']")
                if links.count() == 0:
                    continue
                href = links.first.get_attribute("href")
                if href:
                    job_url = urljoin("https://www.linkedin.com", href).split("?")[0]
                    if collect_card_diagnostics:
                        linkedin_job_id = self._linkedin_job_id(job_url, card)
                        debug_dir = self._create_easy_apply_debug_dir(
                            job_id=linkedin_job_id,
                            timestamp=debug_timestamp,
                        )
                        self._capture_debug_screenshot(page, debug_dir, "01_job_card_click")
                        click_diagnostic = self._inspect_job_card_click(
                            context,
                            page,
                            card,
                            links.first,
                            job_url,
                            linkedin_job_id,
                            debug_dir,
                        )
                        self._merge_diagnostic_file(debug_dir, click_diagnostic)
                    job_urls.append(job_url)
                    if page.url != url:
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_timeout(500)
            except Exception as exc:
                logger.warning("Skipping LinkedIn result link %s: %s", index, exc)

        for job_url in list(dict.fromkeys(job_urls))[:limit]:
            try:
                page.goto(job_url, wait_until="domcontentloaded")
                readiness = {
                    "domcontentloaded": True,
                    "networkidle": False,
                    "main_content": False,
                    "final_url": page.url,
                }
                try:
                    page.locator("main, .jobs-details, .job-view-layout").first.wait_for(
                        state="visible",
                        timeout=3000,
                    )
                    readiness["main_content"] = True
                except PlaywrightTimeoutError:
                    pass
                readiness["final_url"] = page.url
                linkedin_job_id = self._linkedin_job_id(job_url)
                debug_dir = None
                if collect_card_diagnostics:
                    debug_dir = self._create_easy_apply_debug_dir(
                        job_id=linkedin_job_id,
                        timestamp=debug_timestamp,
                    )
                    self._capture_debug_screenshot(page, debug_dir, "02_job_details_page_loaded")
                    self._capture_debug_screenshot(page, debug_dir, "03_before_easy_apply_detection")
                title = self._first_text(page, [".jobs-unified-top-card__job-title", ".top-card-layout__title", "h1"])
                company = self._first_text(
                    page,
                    [
                        ".jobs-unified-top-card__company-name",
                        ".job-details-jobs-unified-top-card__company-name",
                        ".topcard__org-name-link",
                        ".topcard__flavor",
                    ],
                )
                place = self._first_text(
                    page,
                    [
                        ".jobs-unified-top-card__bullet",
                        ".job-details-jobs-unified-top-card__primary-description",
                        ".topcard__flavor--bullet",
                    ],
                )
                description = self._first_text(
                    page,
                    ["#job-details", ".jobs-description-content__text", ".show-more-less-html__markup"],
                )
                main_text = self._first_text(page, ["main"])
                page_title_parts = [part.strip() for part in page.title().split("|")]
                if not title and page_title_parts:
                    title = page_title_parts[0]
                if not company and len(page_title_parts) > 1:
                    company = page_title_parts[1]
                if not place and main_text:
                    lines = [line.strip() for line in main_text.splitlines() if line.strip()]
                    try:
                        title_index = lines.index(title)
                        place = lines[title_index + 1] if title_index + 1 < len(lines) else ""
                    except ValueError:
                        place = ""
                if not description:
                    description = main_text
                search_job = JobOpportunity(
                    id=0,
                    resume_id=resume.id,
                    title=title or "Unknown LinkedIn job",
                    company=company or "Unknown company",
                    location=place,
                    job_url=job_url,
                    description=description or "",
                    easy_apply=False,
                    match_score=0,
                )
                detected_locator, search_diagnostics = self._detect_easy_apply(
                    page,
                    search_job,
                    readiness,
                )
                easy_apply = detected_locator is not None
                search_diagnostics.update(
                    {
                        "linkedin_job_id": linkedin_job_id,
                        "phase": "search",
                        "search_phase_easy_apply_result": easy_apply,
                        "application_phase_easy_apply_result": None,
                        "EASY_APPLY_MISMATCH": False,
                        "failure_reason": None if easy_apply else search_diagnostics["reason"],
                        "navigation_events": [
                            self._navigation_state(page, "search_job_details_navigation")
                        ],
                    }
                )
                if debug_dir:
                    self._capture_debug_screenshot(page, debug_dir, "04_after_easy_apply_detection")
                    self._merge_diagnostic_file(debug_dir, search_diagnostics)
                if title and company:
                    jobs.append(
                        {
                            "title": title,
                            "company": company,
                            "location": place,
                            "description": description,
                            "job_url": job_url,
                            "easy_apply": easy_apply,
                        }
                    )
            except Exception as exc:
                logger.warning("Skipping LinkedIn job %s: %s", job_url, exc)
        return jobs

    @retry(
        retry=retry_if_exception_type((PlaywrightTimeoutError, RuntimeError)),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def apply_to_job(
        self,
        job: JobOpportunity,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        dry_run: bool,
        capture_evidence: bool = False,
        screening_context: dict | None = None,
    ) -> AutomationResult:
        run_log: list[dict] = []
        contact_events: list[dict] = []
        screening_events: list[dict] = []
        employment_events: list[dict] = []
        screening_context = screening_context or {"mode": "safe", "saved_answers": []}
        evidence_dir = self._create_evidence_dir(job) if capture_evidence else None
        contact_debug_dir: Path | None = None
        profile_apply_debug_dir: Path | None = None
        form_navigation_debug_dir: Path | None = None
        screening_debug_dir: Path | None = None
        workflow_debug_dir: Path | None = None
        employment_history_debug_dir: Path | None = None
        form_steps: list[dict] = []
        workflow_trace: list[dict] = []
        workflow_seen_states: dict[str, int] = {}
        workflow_started_at = datetime.utcnow()
        current_step = "initialized"
        resume_upload_debug_dir, resume_upload_diagnostics = self._create_resume_upload_diagnostics(job, resume)
        self._log(run_log, current_step, f"{job.title} at {job.company}")
        self._log(run_log, "resume_upload_check", json.dumps(resume_upload_diagnostics))
        if resume_upload_diagnostics["mismatch_detected"] or not resume_upload_diagnostics["file_exists"]:
            current_step = "needs_review"
            reason = resume_upload_diagnostics["reason_if_failed"]
            self._log(run_log, current_step, reason)
            self._save_resume_upload_diagnostics(resume_upload_debug_dir, resume_upload_diagnostics)
            return AutomationResult(
                ApplicationStatus.NEEDS_REVIEW,
                reason,
                run_log,
                current_step,
                None,
                resume_upload_diagnostics=resume_upload_diagnostics,
            )
        self._persist_run_log(
            evidence_dir,
            job,
            dry_run,
            current_step,
            run_log,
            contact_events=contact_events,
        )
        try:
            with self._browser_context() as context:
                page = context.new_page()
                current_step = "job_opened"
                page.goto(job.job_url, wait_until="domcontentloaded")
                readiness = self._wait_for_job_page(page)
                navigation = self._navigation_state(page, "application_job_navigation")
                self._log(run_log, current_step, json.dumps(navigation))
                self._capture_screenshot(page, evidence_dir, "01_job_page")
                self._persist_run_log(
                    evidence_dir,
                    job,
                    dry_run,
                    current_step,
                    run_log,
                    contact_events=contact_events,
                )

                linkedin_job_id = self._linkedin_job_id(job.job_url)
                contact_debug_dir = self._create_contact_debug_dir(job, linkedin_job_id)
                profile_apply_debug_dir = self._create_profile_apply_debug_dir(job, linkedin_job_id)
                form_navigation_debug_dir = self._create_form_navigation_debug_dir(job, linkedin_job_id)
                screening_debug_dir = self._create_screening_debug_dir(job, linkedin_job_id)
                workflow_debug_dir = self._create_workflow_debug_dir(job, linkedin_job_id)
                employment_history_debug_dir = self._create_employment_history_debug_dir(job, linkedin_job_id)
                self._save_profile_apply_debug(
                    profile_apply_debug_dir,
                    "merged_candidate_profile.json",
                    self._candidate_profile_debug_payload(candidate_profile),
                )
                self._log(
                    run_log,
                    "linkedin_job_identified",
                    json.dumps(
                        {
                            "linkedin_job_id": linkedin_job_id,
                            "job_url": job.job_url,
                        }
                    ),
                )
                debug_dir = self._create_easy_apply_debug_dir(job_id=linkedin_job_id)
                self._capture_debug_screenshot(page, debug_dir, "02_job_details_page_loaded")
                self._capture_debug_screenshot(page, debug_dir, "03_before_easy_apply_detection")
                easy_apply, diagnostics = self._detect_easy_apply(page, job, readiness)
                if easy_apply is None and job.easy_apply:
                    first_attempt = diagnostics
                    page.reload(wait_until="domcontentloaded")
                    readiness = self._wait_for_job_page(page)
                    self._capture_debug_screenshot(
                        page,
                        debug_dir,
                        "03b_after_reload_before_detection",
                    )
                    easy_apply, diagnostics = self._detect_easy_apply(page, job, readiness)
                    diagnostics["retry_after_reload"] = {
                        "attempted": True,
                        "first_classification": first_attempt["classification"],
                        "first_reason": first_attempt["reason"],
                        "first_page_title": navigation["page_title"],
                    }
                application_easy_apply = easy_apply is not None
                mismatch = bool(job.easy_apply and not application_easy_apply)
                diagnostics.update(
                    {
                        "linkedin_job_id": linkedin_job_id,
                        "phase": "application",
                        "search_phase_easy_apply_result": job.easy_apply,
                        "application_phase_easy_apply_result": application_easy_apply,
                        "EASY_APPLY_MISMATCH": mismatch,
                        "navigation_events": [navigation],
                        "failure_reason": None if application_easy_apply else diagnostics["reason"],
                    }
                )
                self._capture_debug_screenshot(page, debug_dir, "04_after_easy_apply_detection")
                self._log(
                    run_log,
                    "apply_controls_inspected",
                    json.dumps(
                        {
                            "buttons": diagnostics["visible_button_texts"],
                            "selector_results": diagnostics["selector_results"],
                            "classification": diagnostics["classification"],
                            "EASY_APPLY_MISMATCH": mismatch,
                        }
                    ),
                )
                if easy_apply is None:
                    current_step = "easy_apply_unavailable"
                    reason = diagnostics["reason"]
                    self._log(run_log, current_step, reason)
                    self._save_easy_apply_diagnostics(page, debug_dir, diagnostics, save_html=True)
                    self._capture_screenshot(page, evidence_dir, "failure_easy_apply_unavailable")
                    self._persist_run_log(
                        evidence_dir,
                        job,
                        dry_run,
                        current_step,
                        run_log,
                        contact_events=contact_events,
                    )
                    return AutomationResult(
                        ApplicationStatus.SKIPPED,
                        reason,
                        run_log,
                        current_step,
                        str(evidence_dir) if evidence_dir else None,
                    )
                self._save_easy_apply_diagnostics(page, debug_dir, diagnostics, save_html=False)
                easy_apply.click()
                modal = self._wait_for_easy_apply_modal(page)
                if modal is None:
                    current_step = "easy_apply_modal_unavailable"
                    message = "Easy Apply was clicked, but the application modal did not become available."
                    self._log(run_log, current_step, message)
                    self._capture_screenshot(page, evidence_dir, "failure_modal_unavailable")
                    self._save_form_diagnostics(page, None, evidence_dir, debug_dir, current_step)
                    self._persist_run_log(
                        evidence_dir,
                        job,
                        dry_run,
                        current_step,
                        run_log,
                        contact_events=contact_events,
                    )
                    return AutomationResult(
                        ApplicationStatus.NEEDS_REVIEW,
                        message,
                        run_log,
                        current_step,
                        str(evidence_dir) if evidence_dir else None,
                    )
                current_step = "easy_apply_clicked"
                self._log(run_log, current_step, job.job_url)
                self._capture_screenshot(page, evidence_dir, "02_easy_apply_modal")

                resume_path = Path(resume.stored_path).resolve()
                if self._upload_resume_if_needed(
                    modal,
                    resume_path,
                    resume_upload_diagnostics,
                    resume_upload_debug_dir,
                ):
                    current_step = "resume_uploaded"
                    self._log(run_log, current_step, resume.filename)
                    self._log(run_log, "resume_upload_check", json.dumps(resume_upload_diagnostics))
                    page.wait_for_timeout(1200)
                    self._capture_screenshot(page, evidence_dir, "03_resume_upload")
                if resume_upload_diagnostics.get("mismatch_detected"):
                    current_step = "needs_review"
                    message = resume_upload_diagnostics.get("reason_if_failed") or "Resume upload mismatch detected."
                    self._log(run_log, current_step, message)
                    self._persist_run_log(
                        evidence_dir,
                        job,
                        dry_run,
                        current_step,
                        run_log,
                        contact_events=contact_events,
                    )
                    return AutomationResult(
                        ApplicationStatus.NEEDS_REVIEW,
                        message,
                        run_log,
                        current_step,
                        str(evidence_dir) if evidence_dir else None,
                        resume_upload_diagnostics=resume_upload_diagnostics,
                    )

                for form_page in range(1, 9):
                    modal = self._current_easy_apply_modal(page)
                    if modal is None:
                        current_step = "easy_apply_modal_closed"
                        message = "The Easy Apply modal closed before the workflow completed."
                        self._log(run_log, current_step, message)
                        self._record_form_navigation_step(
                            page,
                            None,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            "none",
                            "modal_closed",
                            "LinkedIn modal closed unexpectedly",
                        )
                        self._save_form_diagnostics(page, None, evidence_dir, debug_dir, current_step)
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            message,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                            screening_events,
                        )

                    inspection = self._record_workflow_trace(
                        page,
                        modal,
                        workflow_debug_dir,
                        workflow_trace,
                        workflow_seen_states,
                        form_page,
                        "inspect",
                        False,
                        None,
                        0,
                        workflow_started_at,
                    )
                    if inspection["repeated_count"] >= 3:
                        current_step = "needs_review"
                        message = (
                            "Repeated workflow state detected: "
                            f"{inspection['detected_page_type']} page repeated "
                            f"{inspection['repeated_count']} times without meaningful navigation."
                        )
                        self._log(run_log, current_step, message)
                        self._record_form_navigation_step(
                            page,
                            modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            "none",
                            "stopped",
                            message,
                        )
                        self._capture_screenshot(page, evidence_dir, "failure_repeated_workflow_state")
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            message,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                            screening_events,
                            resume_upload_diagnostics,
                        )

                    if self._upload_resume_if_needed(
                        modal,
                        resume_path,
                        resume_upload_diagnostics,
                        resume_upload_debug_dir,
                    ):
                        current_step = "resume_uploaded"
                        self._log(run_log, current_step, resume.filename)
                        self._log(run_log, "resume_upload_check", json.dumps(resume_upload_diagnostics))
                        page.wait_for_timeout(1200)
                        self._capture_screenshot(page, evidence_dir, "03_resume_upload")
                    if resume_upload_diagnostics.get("mismatch_detected"):
                        current_step = "needs_review"
                        message = resume_upload_diagnostics.get("reason_if_failed") or "Resume upload mismatch detected."
                        self._log(run_log, current_step, message)
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            message,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                            screening_events,
                            resume_upload_diagnostics,
                        )

                    contact_missing = self._fill_contact_fields(
                        modal,
                        candidate_profile,
                        resume,
                        contact_debug_dir,
                        profile_apply_debug_dir,
                        contact_events,
                    )
                    if contact_missing:
                        current_step = "needs_review"
                        message = (
                            "Required contact fields need review: "
                            f"{', '.join(contact_missing)}."
                        )
                        self._log(run_log, current_step, message)
                        self._record_form_navigation_step(
                            page,
                            modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            "none",
                            "stopped",
                            f"Missing required field: {', '.join(contact_missing)}",
                        )
                        self._capture_screenshot(page, evidence_dir, "failure_missing_contact_fields")
                        self._save_form_diagnostics(
                            page,
                            modal,
                            evidence_dir,
                            debug_dir,
                            current_step,
                        )
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            message,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                            screening_events,
                        )

                    answered_screening = self._answer_screening_questions(
                        page,
                        modal,
                        resume,
                        job,
                        candidate_profile,
                        screening_context,
                        screening_debug_dir,
                        screening_events,
                    )
                    if answered_screening:
                        current_step = "screening_questions_answered"
                        self._log(run_log, current_step, json.dumps(answered_screening))
                        page.wait_for_timeout(500)

                    employment_missing = self._fill_employment_history_fields(
                        modal,
                        screening_context.get("employment_history") or [],
                        candidate_profile,
                        resume,
                        screening_context.get("mode") or "safe",
                        employment_history_debug_dir,
                        employment_events,
                    )
                    if employment_events:
                        latest_employment_events = employment_events[-10:]
                        self._log(
                            run_log,
                            "employment_history_autofill",
                            json.dumps(latest_employment_events, default=str),
                        )
                        page.wait_for_timeout(500)
                    if employment_missing:
                        current_step = "needs_review"
                        message = "Required employment fields need review: " + ", ".join(employment_missing[:8]) + "."
                        self._log(run_log, current_step, message)
                        self._record_form_navigation_step(
                            page,
                            modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            "none",
                            "stopped",
                            message,
                        )
                        self._capture_screenshot(page, evidence_dir, "failure_missing_employment_fields")
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            message,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                            screening_events,
                            resume_upload_diagnostics,
                        )

                    blocker = self._detect_unanswered_custom_questions(modal)
                    if blocker:
                        current_step = "needs_review"
                        self._log(run_log, current_step, blocker)
                        self._record_form_navigation_step(
                            page,
                            modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            "none",
                            "stopped",
                            f"Screening question unanswered: {blocker}",
                        )
                        self._capture_screenshot(page, evidence_dir, "failure_needs_review")
                        self._save_form_diagnostics(
                            page,
                            modal,
                            evidence_dir,
                            debug_dir,
                            current_step,
                        )
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            blocker,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                            screening_events,
                        )

                    validation_messages = self._validation_messages(modal)
                    if validation_messages and employment_events and not self._form_required_fields(modal, unresolved_only=True):
                        validation_messages = []
                    if validation_messages:
                        current_step = "needs_review"
                        message = "Validation error: " + "; ".join(validation_messages[:5])
                        self._log(run_log, current_step, message)
                        self._record_workflow_trace(
                            page,
                            modal,
                            workflow_debug_dir,
                            workflow_trace,
                            workflow_seen_states,
                            form_page,
                            "none",
                            False,
                            None,
                            0,
                            workflow_started_at,
                            message,
                        )
                        self._record_form_navigation_step(
                            page,
                            modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            "none",
                            "stopped",
                            message,
                        )
                        self._capture_screenshot(page, evidence_dir, "failure_validation_error")
                        self._save_form_diagnostics(
                            page,
                            modal,
                            evidence_dir,
                            debug_dir,
                            current_step,
                        )
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            message,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                            screening_events,
                            resume_upload_diagnostics,
                        )

                    submit = modal.get_by_role(
                        "button",
                        name=re.compile(r"^\s*Submit application\s*$", re.I),
                    )
                    if submit.count() > 0 and submit.first.is_enabled():
                        current_step = "review_page_reached"
                        self._log(run_log, current_step, f"Form page {form_page}")
                        self._capture_screenshot(page, evidence_dir, "04_review_step")
                        self._record_form_navigation_step(
                            page,
                            modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            "submit_available",
                            "dry_run_stop" if dry_run else "submit_ready",
                            None if not dry_run else "Dry run reached submit/review stage; application not submitted.",
                        )
                        if dry_run:
                            current_step = "dry_run_complete"
                            message = "Dry run reached submit/review stage; application not submitted."
                            self._log(run_log, current_step, message)
                            self._persist_run_log(
                                evidence_dir,
                                job,
                                dry_run,
                                current_step,
                                run_log,
                                contact_events=contact_events,
                            )
                            return AutomationResult(
                                ApplicationStatus.SKIPPED,
                                message,
                                run_log,
                                current_step,
                                str(evidence_dir) if evidence_dir else None,
                                self._contact_detected(contact_events),
                                self._contact_filled(contact_events),
                                self._contact_missing(contact_events),
                                screening_events,
                            )

                        submit.first.click()
                        current_step = "submit_clicked"
                        self._log(run_log, current_step, job.job_url)
                        page.wait_for_timeout(2500)
                        self._capture_screenshot(page, evidence_dir, "05_submission_confirmation")
                        if self._confirmation_received(page):
                            current_step = "confirmation_received"
                            self._log(run_log, current_step, job.job_url)
                            self._persist_run_log(
                                evidence_dir,
                                job,
                                dry_run,
                                current_step,
                                run_log,
                                contact_events=contact_events,
                            )
                            return AutomationResult(
                                ApplicationStatus.APPLIED,
                                None,
                                run_log,
                                current_step,
                                str(evidence_dir) if evidence_dir else None,
                                self._contact_detected(contact_events),
                                self._contact_filled(contact_events),
                                self._contact_missing(contact_events),
                                screening_events,
                            )

                        current_step = "confirmation_not_detected"
                        message = "Submit was clicked, but LinkedIn confirmation was not detected."
                        self._log(run_log, current_step, message)
                        self._capture_screenshot(page, evidence_dir, "failure_confirmation_not_detected")
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        return AutomationResult(
                            ApplicationStatus.NEEDS_REVIEW,
                            message,
                            run_log,
                            current_step,
                            str(evidence_dir) if evidence_dir else None,
                            self._contact_detected(contact_events),
                            self._contact_filled(contact_events),
                            self._contact_missing(contact_events),
                        )

                    navigation = self._find_navigation_button(modal)
                    if navigation["locator"] is not None and navigation["enabled"]:
                        button_text = navigation["text"]
                        before_state = self._workflow_state(page, modal)
                        self._record_form_navigation_step(
                            page,
                            modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            f"click_{button_text.lower()}",
                            "attempting_click",
                            None,
                        )
                        try:
                            navigation["locator"].click(timeout=8000)
                        except PlaywrightTimeoutError:
                            navigation["locator"].click(timeout=3000, force=True)
                        self._wait_for_form_transition(page, modal)
                        after_modal = self._current_easy_apply_modal(page)
                        after_state = self._workflow_state(page, after_modal)
                        navigation_failure = self._navigation_failure_reason(before_state, after_state, button_text)
                        if navigation_failure:
                            answered_after_navigation = self._answer_screening_questions(
                                page,
                                after_modal,
                                resume,
                                job,
                                candidate_profile,
                                screening_context,
                                screening_debug_dir,
                                screening_events,
                            )
                            if answered_after_navigation:
                                current_step = "screening_questions_answered"
                                self._log(run_log, current_step, json.dumps(answered_after_navigation))
                                page.wait_for_timeout(500)
                                unresolved_after_screening = self._detect_unanswered_custom_questions(after_modal)
                                if not unresolved_after_screening:
                                    self._record_form_navigation_step(
                                        page,
                                        after_modal,
                                        form_navigation_debug_dir,
                                        form_steps,
                                        form_page,
                                        f"clicked_{button_text.lower()}",
                                        "screening_answered_after_navigation",
                                        None,
                                    )
                                    self._persist_run_log(
                                        evidence_dir,
                                        job,
                                        dry_run,
                                        current_step,
                                        run_log,
                                        contact_events=contact_events,
                                    )
                                    continue
                                navigation_failure = f"Navigation revealed unresolved required fields: {unresolved_after_screening}"
                            current_step = "needs_review"
                            self._log(run_log, current_step, navigation_failure)
                            self._record_workflow_trace(
                                page,
                                after_modal,
                                workflow_debug_dir,
                                workflow_trace,
                                workflow_seen_states,
                                form_page,
                                f"click_{button_text.lower()}",
                                False,
                                after_state["detected_page_type"],
                                1,
                                workflow_started_at,
                                navigation_failure,
                            )
                            self._record_form_navigation_step(
                                page,
                                after_modal,
                                form_navigation_debug_dir,
                                form_steps,
                                form_page,
                                f"clicked_{button_text.lower()}",
                                "navigation_failed",
                                navigation_failure,
                            )
                            self._capture_screenshot(page, evidence_dir, "failure_navigation_unchanged")
                            self._persist_run_log(
                                evidence_dir,
                                job,
                                dry_run,
                                current_step,
                                run_log,
                                contact_events=contact_events,
                            )
                            return AutomationResult(
                                ApplicationStatus.NEEDS_REVIEW,
                                navigation_failure,
                                run_log,
                                current_step,
                                str(evidence_dir) if evidence_dir else None,
                                self._contact_detected(contact_events),
                                self._contact_filled(contact_events),
                                self._contact_missing(contact_events),
                                screening_events,
                                resume_upload_diagnostics,
                            )
                        current_step = "form_page_processed"
                        self._log(run_log, current_step, f"Page {form_page}: {button_text}")
                        if "review" in button_text.lower():
                            self._capture_screenshot(page, evidence_dir, "04_review_step")
                        self._record_form_navigation_step(
                            page,
                            after_modal,
                            form_navigation_debug_dir,
                            form_steps,
                            form_page,
                            f"clicked_{button_text.lower()}",
                            "advanced_or_transitioned",
                            None,
                        )
                        self._record_workflow_trace(
                            page,
                            after_modal,
                            workflow_debug_dir,
                            workflow_trace,
                            workflow_seen_states,
                            form_page,
                            f"click_{button_text.lower()}",
                            True,
                            after_state["detected_page_type"],
                            0,
                            workflow_started_at,
                        )
                        self._persist_run_log(
                            evidence_dir,
                            job,
                            dry_run,
                            current_step,
                            run_log,
                            contact_events=contact_events,
                        )
                        continue

                    if navigation["locator"] is not None and not navigation["enabled"]:
                        reason = f"Disabled button: {navigation['text']}"
                    else:
                        reason = self._classify_form_stop_reason(modal)

                    current_step = "needs_review"
                    message = reason
                    self._log(run_log, current_step, message)
                    self._record_form_navigation_step(
                        page,
                        modal,
                        form_navigation_debug_dir,
                        form_steps,
                        form_page,
                        "none",
                        "stopped",
                        reason,
                    )
                    self._capture_screenshot(page, evidence_dir, "failure_form_incomplete")
                    self._save_form_diagnostics(
                        page,
                        modal,
                        evidence_dir,
                        debug_dir,
                        current_step,
                    )
                    self._persist_run_log(
                        evidence_dir,
                        job,
                        dry_run,
                        current_step,
                        run_log,
                        contact_events=contact_events,
                    )
                    return AutomationResult(
                        ApplicationStatus.NEEDS_REVIEW,
                        message,
                        run_log,
                        current_step,
                        str(evidence_dir) if evidence_dir else None,
                        self._contact_detected(contact_events),
                        self._contact_filled(contact_events),
                        self._contact_missing(contact_events),
                    )

                current_step = "needs_review"
                modal = self._current_easy_apply_modal(page)
                message = self._step_limit_reason(modal)
                self._log(run_log, current_step, message)
                self._record_workflow_trace(
                    page,
                    modal,
                    workflow_debug_dir,
                    workflow_trace,
                    workflow_seen_states,
                    9,
                    "none",
                    False,
                    None,
                    0,
                    workflow_started_at,
                    message,
                )
                self._capture_screenshot(page, evidence_dir, "failure_step_limit")
                self._persist_run_log(
                    evidence_dir,
                    job,
                    dry_run,
                    current_step,
                    run_log,
                    contact_events=contact_events,
                )
                return AutomationResult(
                    ApplicationStatus.NEEDS_REVIEW,
                    message,
                    run_log,
                    current_step,
                    str(evidence_dir) if evidence_dir else None,
                    self._contact_detected(contact_events),
                    self._contact_filled(contact_events),
                    self._contact_missing(contact_events),
                )
        except Exception as exc:
            current_step = "automation_failed"
            self._log(run_log, current_step, str(exc))
            if "page" in locals():
                self._capture_screenshot(page, evidence_dir, "failure_exception")
                if "debug_dir" in locals():
                    self._save_form_diagnostics(
                        page,
                        self._current_easy_apply_modal(page),
                        evidence_dir,
                        debug_dir,
                        current_step,
                    )
            self._persist_run_log(
                evidence_dir,
                job,
                dry_run,
                current_step,
                run_log,
                error=str(exc),
                contact_events=contact_events,
            )
            return AutomationResult(
                ApplicationStatus.FAILED,
                str(exc),
                run_log,
                current_step,
                str(evidence_dir) if evidence_dir else None,
                self._contact_detected(contact_events),
                self._contact_filled(contact_events),
                self._contact_missing(contact_events),
            )

    def _wait_for_job_page(self, page: Page) -> dict:
        readiness = {
            "domcontentloaded": True,
            "networkidle": False,
            "main_content": False,
            "final_url": page.url,
        }
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
            readiness["networkidle"] = True
        except PlaywrightTimeoutError:
            logger.info("LinkedIn job page did not reach network idle within 10 seconds.")
        try:
            page.locator("main, .jobs-details, .job-view-layout").first.wait_for(
                state="visible",
                timeout=10000,
            )
            readiness["main_content"] = True
        except PlaywrightTimeoutError:
            logger.info("LinkedIn job main content was not confirmed within 10 seconds.")
        page.wait_for_timeout(750)
        readiness["final_url"] = page.url
        return readiness

    def _detect_easy_apply(self, page: Page, job: JobOpportunity, readiness: dict):
        exact_selectors = [
            'button:has-text("Easy Apply")',
            'button:has-text("Apply")',
            'button:has-text("Apply now")',
            'button:has-text("Submit application")',
        ]
        exact_selector_results = [
            {
                "selector": selector,
                "count": page.locator(selector).count(),
            }
            for selector in exact_selectors
        ]
        selector_definitions = [
            (
                "css_button_has_text_easy_apply",
                page.locator('button:has-text("Easy Apply")'),
                True,
            ),
            (
                "role_button_easy_apply",
                page.get_by_role("button", name=re.compile(r"^\s*Easy Apply\s*$", re.I)),
                True,
            ),
            (
                "aria_label_easy_apply",
                page.locator("button[aria-label*='Easy Apply' i], a[aria-label*='Easy Apply' i]"),
                True,
            ),
            (
                "linkedin_inapply_control",
                page.locator(
                    "button[data-control-name*='inapply' i], "
                    "a[data-control-name*='inapply' i], "
                    "button.jobs-apply-button"
                ),
                True,
            ),
            (
                "role_button_apply_now",
                page.get_by_role("button", name=re.compile(r"^\s*Apply now\s*$", re.I)),
                False,
            ),
            (
                "role_button_apply",
                page.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I)),
                False,
            ),
            (
                "role_button_submit_application",
                page.get_by_role("button", name=re.compile(r"^\s*Submit application\s*$", re.I)),
                False,
            ),
        ]
        selector_results: list[dict] = []
        confirmed_locator = None
        for name, locator, explicit_easy_apply in selector_definitions:
            count = locator.count()
            selector_results.append(
                {
                    "name": name,
                    "count": count,
                    "explicit_easy_apply": explicit_easy_apply,
                }
            )
            if confirmed_locator is None and explicit_easy_apply and count > 0:
                confirmed_locator = locator.first

        visible_buttons = self._visible_button_texts(page)
        external_links = self._external_apply_links(page)
        final_host = urlparse(page.url).hostname or ""
        redirected_external = bool(final_host and not final_host.endswith("linkedin.com"))

        if confirmed_locator is not None:
            reason = "Confirmed LinkedIn Easy Apply control."
            classification = "linkedin_easy_apply"
        elif redirected_external:
            reason = f"Job page redirected to external host: {final_host}."
            classification = "external_redirect"
        elif external_links:
            reason = "Only an external application link was detected; LinkedIn Easy Apply was not confirmed."
            classification = "external_application"
        elif any(
            result["count"] > 0
            for result in selector_results
            if result["name"] in {"role_button_apply", "role_button_apply_now"}
        ):
            reason = "A generic Apply button was found, but it could not be verified as LinkedIn Easy Apply."
            classification = "unverified_apply_button"
        elif not job.easy_apply:
            reason = "LinkedIn page did not expose Easy Apply and the search result was not marked Easy Apply."
            classification = "not_easy_apply"
        else:
            reason = "Job was marked Easy Apply, but no supported Easy Apply control was found after page readiness checks."
            classification = "easy_apply_control_missing"

        diagnostics = {
            "job_id": job.id,
            "job_title": job.title,
            "company": job.company,
            "job_url": job.job_url,
            "final_url": page.url,
            "recorded_easy_apply": job.easy_apply,
            "readiness": readiness,
            "visible_button_texts": visible_buttons,
            "selector_results": selector_results,
            "exact_selector_matches": exact_selector_results,
            "external_apply_links": external_links,
            "classification": classification,
            "reason": reason,
            "captured_at": datetime.utcnow().isoformat(),
        }
        return confirmed_locator, diagnostics

    def _inspect_job_card_click(
        self,
        context: BrowserContext,
        page: Page,
        card,
        link,
        job_url: str,
        linkedin_job_id: str,
        debug_dir: Path,
    ) -> dict:
        before_pages = list(context.pages)
        before_navigation = self._navigation_state(page, "before_job_card_click")
        card_attributes = {
            "class": card.get_attribute("class"),
            "aria_current": card.get_attribute("aria-current"),
            "data_job_id": card.get_attribute("data-occludable-job-id"),
        }
        link.click()
        page.wait_for_timeout(1500)
        after_pages = list(context.pages)
        new_pages = [candidate for candidate in after_pages if candidate not in before_pages]
        opened_page = new_pages[-1] if new_pages else page
        try:
            opened_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeoutError:
            pass
        after_navigation = self._navigation_state(opened_page, "after_job_card_click")
        host = urlparse(opened_page.url).hostname or ""
        if host and not host.endswith("linkedin.com"):
            opening_mode = "external_company_site"
        elif new_pages:
            opening_mode = "new_tab"
        else:
            opening_mode = "in_place_details_panel"

        selected_state = False
        if opened_page is page and page.url.startswith("https://www.linkedin.com/jobs/search"):
            selected_card = page.locator(
                f"li[data-occludable-job-id='{linkedin_job_id}'], "
                f"[data-job-id='{linkedin_job_id}']"
            )
            if selected_card.count() > 0:
                selected = selected_card.first
                class_name = selected.get_attribute("class") or ""
                selected_state = any(
                    (
                        "selected" in class_name.lower(),
                        selected.get_attribute("aria-current") in {"true", "page"},
                        selected.get_attribute("aria-selected") == "true",
                    )
                )
        elif opened_page.url:
            selected_state = linkedin_job_id in opened_page.url

        self._capture_debug_screenshot(
            opened_page,
            debug_dir,
            "02_job_details_page_loaded",
        )
        if new_pages:
            opened_page.close()

        return {
            "linkedin_job_id": linkedin_job_id,
            "job_url": job_url,
            "phase": "search_card_click",
            "card_attributes_before_click": card_attributes,
            "selected_job_card_active_after_click": selected_state,
            "opening_mode": opening_mode,
            "navigation_events": [before_navigation, after_navigation],
            "captured_at": datetime.utcnow().isoformat(),
        }

    def _navigation_state(self, page: Page, event: str) -> dict:
        try:
            title = page.title()
        except Exception:
            title = ""
        return {
            "event": event,
            "page_title": title,
            "current_url": page.url,
            "captured_at": datetime.utcnow().isoformat(),
        }

    def _linkedin_job_id(self, job_url: str, card=None) -> str:
        if card is not None:
            for attribute in ("data-occludable-job-id", "data-job-id"):
                value = card.get_attribute(attribute)
                if value:
                    match = re.search(r"\d+", value)
                    if match:
                        return match.group(0)
        match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)(?:[/?]|$)", job_url)
        if match:
            return match.group(1)
        query_match = re.search(r"(?:currentJobId|jobId)=(\d+)", job_url)
        if query_match:
            return query_match.group(1)
        fallback = re.sub(r"[^a-zA-Z0-9_-]+", "_", job_url).strip("_")
        return fallback[-80:] or "unknown_job"

    def _visible_button_texts(self, page: Page) -> list[str]:
        texts = page.locator("button:visible, a[role='button']:visible").all_inner_texts()
        return list(dict.fromkeys(text.strip() for text in texts if text.strip()))

    def _external_apply_links(self, page: Page) -> list[dict]:
        links = page.locator(
            "a[href]:visible:has-text('Apply'), "
            "a[href]:visible[aria-label*='Apply' i]"
        )
        results: list[dict] = []
        for index in range(links.count()):
            link = links.nth(index)
            href = link.get_attribute("href") or ""
            absolute_url = urljoin(page.url, href)
            host = urlparse(absolute_url).hostname or ""
            if host and not host.endswith("linkedin.com"):
                results.append(
                    {
                        "text": link.inner_text().strip(),
                        "url": absolute_url,
                        "host": host,
                    }
                )
        return results

    def _create_easy_apply_debug_dir(
        self,
        job_id: str,
        timestamp: str | None = None,
    ) -> Path:
        timestamp = timestamp or datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_job_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(job_id)).strip("_") or "unknown_job"
        debug_dir = self.settings.easy_apply_debug_dir / timestamp / safe_job_id
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _capture_debug_screenshot(self, page: Page, debug_dir: Path, name: str) -> None:
        try:
            try:
                page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True, timeout=5000)
            except TypeError:
                page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
        except Exception as exc:
            logger.warning("Could not capture Easy Apply debug screenshot: %s", exc)

    def _save_easy_apply_diagnostics(
        self,
        page: Page,
        debug_dir: Path,
        diagnostics: dict,
        save_html: bool,
    ) -> None:
        (debug_dir / "diagnostic.json").write_text(
            json.dumps(diagnostics, indent=2),
            encoding="utf-8",
        )
        if save_html:
            try:
                (debug_dir / "page.html").write_text(page.content(), encoding="utf-8")
            except Exception as exc:
                logger.warning("Could not save Easy Apply page HTML: %s", exc)

    def _merge_diagnostic_file(self, debug_dir: Path, updates: dict) -> None:
        path = debug_dir / "diagnostic.json"
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        existing.update(updates)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    @contextmanager
    def _browser_context(self) -> Iterator[BrowserContext]:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=self.settings.linkedin_headless,
            slow_mo=self.settings.linkedin_slow_mo_ms,
        )
        kwargs = {}
        if self.settings.linkedin_storage_state.exists():
            kwargs["storage_state"] = str(self.settings.linkedin_storage_state)
        context = browser.new_context(**kwargs)
        try:
            yield context
        finally:
            context.close()
            browser.close()
            playwright.stop()

    def _first_text(self, page: Page, selectors: list[str]) -> str:
        for selector in selectors:
            locator = page.locator(selector)
            if locator.count() > 0:
                text = locator.first.inner_text(timeout=2000).strip()
                if text:
                    return text
        return ""

    def _wait_for_easy_apply_modal(self, page: Page):
        self._dismiss_interfering_dialogs(page)
        selectors = [
            ".jobs-easy-apply-modal",
            "[role='dialog'][aria-modal='true']",
            "[data-test-modal]",
        ]
        for _ in range(20):
            for selector in selectors:
                locator = page.locator(selector)
                modal = self._best_easy_apply_modal(locator, prefer_first=selector == ".jobs-easy-apply-modal")
                if modal is not None:
                    return modal
            page.wait_for_timeout(250)
            self._dismiss_interfering_dialogs(page)
        return None

    def _current_easy_apply_modal(self, page: Page):
        self._dismiss_interfering_dialogs(page)
        for selector in (
            ".jobs-easy-apply-modal",
            "[role='dialog'][aria-modal='true']",
            "[data-test-modal]",
        ):
            locator = page.locator(selector)
            modal = self._best_easy_apply_modal(locator, prefer_first=selector == ".jobs-easy-apply-modal")
            if modal is not None:
                return modal
        return None

    def _best_easy_apply_modal(self, locator, prefer_first: bool = False):
        try:
            count = locator.count()
        except Exception:
            return None
        if count <= 0:
            return None
        visible_modals = []
        for index in range(count):
            try:
                modal = locator.nth(index)
                if modal.is_visible():
                    visible_modals.append(modal)
            except Exception:
                continue
        if not visible_modals:
            return None
        if prefer_first:
            return visible_modals[0]
        for modal in visible_modals:
            if self._looks_like_easy_apply_modal(modal):
                return modal
        return visible_modals[0]

    def _looks_like_easy_apply_modal(self, modal) -> bool:
        title = self._modal_title(modal).lower()
        if title.startswith("apply to "):
            return True
        try:
            text = modal.inner_text(timeout=1000).lower()
        except Exception:
            text = ""
        easy_apply_markers = (
            "easy apply",
            "submitting this application",
            "application powered by",
            "contact info",
            "resume",
        )
        feedback_markers = ("help us improve", "sharing feedback", "question 1 of")
        return any(marker in text for marker in easy_apply_markers) and not any(
            marker in text for marker in feedback_markers
        )

    def _dismiss_interfering_dialogs(self, page: Page) -> None:
        dialogs = page.locator("[role='dialog'][aria-modal='true'], [data-test-modal]")
        try:
            count = dialogs.count()
        except Exception:
            return
        for index in range(count):
            try:
                dialog = dialogs.nth(index)
                if not dialog.is_visible() or self._looks_like_easy_apply_modal(dialog):
                    continue
                text = dialog.inner_text(timeout=1000).lower()
            except Exception:
                continue
            if not any(marker in text for marker in ("help us improve", "sharing feedback", "question 1 of")):
                continue
            for close_selector in (
                "button[aria-label*='Dismiss' i]:visible",
                "button[aria-label*='Close' i]:visible",
                "button:has-text('×'):visible",
                "button:has-text('X'):visible",
            ):
                close_button = dialog.locator(close_selector)
                if close_button.count() > 0:
                    try:
                        close_button.first.click(timeout=2000)
                        page.wait_for_timeout(500)
                        break
                    except Exception:
                        continue

    def _wait_for_form_transition(self, page: Page, previous_modal) -> None:
        previous_text = ""
        try:
            previous_text = previous_modal.inner_text(timeout=1500)
        except Exception:
            pass
        for _ in range(20):
            page.wait_for_timeout(200)
            modal = self._current_easy_apply_modal(page)
            if modal is None:
                return
            try:
                current_text = modal.inner_text(timeout=1000)
            except Exception:
                continue
            if current_text != previous_text:
                return

    def _find_navigation_button(self, modal) -> dict:
        if self._looks_like_employment_edit_form(modal):
            save = modal.get_by_role("button", name=re.compile(r"^\s*Save\s*$", re.I))
            if save.count() > 0:
                button = save.first
                return {
                    "text": "Save",
                    "locator": button,
                    "enabled": button.is_enabled(),
                }
        for text in ("Next", "Continue", "Review"):
            locator = modal.get_by_role("button", name=re.compile(rf"^\s*{text}\s*$", re.I))
            if locator.count() > 0:
                button = locator.first
                return {
                    "text": text,
                    "locator": button,
                    "enabled": button.is_enabled(),
                }
        buttons = modal.locator("button:visible")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                label = re.sub(r"\s+", " ", button.inner_text().strip())
            except Exception:
                continue
            if label.lower() in {"next", "continue", "review"}:
                return {
                    "text": label,
                    "locator": button,
                    "enabled": button.is_enabled(),
                }
        return {"text": None, "locator": None, "enabled": False}

    def _looks_like_employment_edit_form(self, modal) -> bool:
        try:
            text = modal.inner_text(timeout=1000).lower()
        except Exception:
            text = ""
        if any(term in text for term in ("i currently work here", "your title", "company", "month of from", "month of to")):
            buttons = {button["text"].lower() for button in self._modal_buttons(modal)}
            return "save" in buttons
        fields = modal.locator("input:visible, textarea:visible, select:visible")
        for index in range(fields.count()):
            field = fields.nth(index)
            if self._employment_field_key(self._field_label(field, index), field):
                buttons = {button["text"].lower() for button in self._modal_buttons(modal)}
                return "save" in buttons
        return False

    def _classify_form_stop_reason(self, modal) -> str:
        unresolved = self._form_required_fields(modal, unresolved_only=True)
        if unresolved:
            return "Missing required field: " + ", ".join(field["label"] for field in unresolved[:8])
        validation_messages = self._validation_messages(modal)
        if validation_messages:
            return "Validation error: " + "; ".join(validation_messages[:5])
        if self._resume_upload_status(modal)["resume_upload_present"] and not self._resume_upload_status(modal)["selected_resume_present"]:
            return "Resume upload missing"
        buttons = self._modal_buttons(modal)
        disabled = [
            button["text"]
            for button in buttons
            if button["text"].lower() in {"next", "continue", "review", "submit application"}
            and not button["enabled"]
        ]
        if disabled:
            return "Disabled button: " + ", ".join(disabled)
        if self._looks_like_screening_page(modal):
            return "Screening question unanswered"
        return "Button not found: no enabled Next, Continue, Review, or Submit application control was available."

    def _create_workflow_debug_dir(self, job: JobOpportunity, linkedin_job_id: str | None) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_title = re.sub(r"[^A-Za-z0-9_.-]+", "_", job.title or "job").strip("_")[:80]
        job_key = linkedin_job_id or str(job.id)
        debug_dir = self.settings.workflow_debug_dir / timestamp / f"{job_key}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _create_employment_history_debug_dir(self, job: JobOpportunity, linkedin_job_id: str | None) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_title = re.sub(r"[^A-Za-z0-9_.-]+", "_", job.title or "job").strip("_")[:80]
        job_key = linkedin_job_id or str(job.id)
        debug_dir = self.settings.employment_history_debug_dir / timestamp / f"{job_key}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _fill_employment_history_fields(
        self,
        modal,
        employment_records: list[dict],
        candidate_profile: CandidateProfile | None,
        resume: ResumeProfile | None,
        mode: str,
        debug_dir: Path | None,
        employment_events: list[dict],
    ) -> list[str]:
        fields = modal.locator("input:visible, textarea:visible, select:visible")
        field_records: list[dict] = []
        mapping_records: list[dict] = []
        fill_records: list[dict] = []
        estimate_records: list[dict] = []
        validation_records: list[dict] = []
        missing: list[str] = []
        occurrence_by_key: dict[str, int] = {}
        for index in range(fields.count()):
            field = fields.nth(index)
            label = self._field_label(field, index)
            key = self._employment_field_key(label, field)
            if key is None:
                continue
            state = self._field_state(field)
            occurrence = occurrence_by_key.get(key, 0)
            occurrence_by_key[key] = occurrence + 1
            record = employment_records[min(occurrence, max(len(employment_records) - 1, 0))] if employment_records else {}
            field_record = {
                "field_label": label,
                "field_key": key,
                "field_type": state["field_type"],
                "required": state["required"],
                "existing_value_present": state["has_value"],
                "record_index": occurrence,
            }
            field_records.append(field_record)
            if state["has_value"] and key != "location":
                event = {
                    **field_record,
                    "action": "kept_existing_value",
                    "success": True,
                    "mapped_value": None,
                    "source": "LinkedIn Prefill",
                    "confidence": "High",
                    "estimated": False,
                }
                employment_events.append(event)
                fill_records.append(event)
                continue
            value_payload = self._employment_value_for_key(key, record, candidate_profile, resume, mode, employment_records)
            if key == "location":
                modal_location = self._extract_location_from_modal(modal)
                if modal_location and (
                    not value_payload.get("value")
                    or len(modal_location) > len(str(value_payload.get("value")))
                ):
                    value_payload = {
                        "value": modal_location,
                        "source": "LinkedIn Easy Apply Header",
                        "confidence": 85,
                        "estimated": False,
                        "reason": "Mapped employment location from LinkedIn modal header.",
                    }
            mapping_records.append(
                {
                    **field_record,
                    "original_value": record.get(key),
                    "mapped_value": value_payload.get("value"),
                    "source": value_payload.get("source"),
                    "confidence": value_payload.get("confidence"),
                    "estimated": value_payload.get("estimated"),
                    "reason": value_payload.get("reason"),
                }
            )
            if value_payload.get("estimated"):
                estimate_records.append({**field_record, **value_payload})
            value = value_payload.get("value")
            if value is None or str(value).strip() == "":
                event = {
                    **field_record,
                    "action": "missing_value",
                    "success": False,
                    "mapped_value": None,
                    "source": value_payload.get("source"),
                    "confidence": value_payload.get("confidence"),
                    "estimated": value_payload.get("estimated"),
                    "reason": value_payload.get("reason") or "No employment history value available.",
                }
                employment_events.append(event)
                fill_records.append(event)
                if state["required"]:
                    missing.append(label)
                continue
            try:
                self._fill_employment_field(field, state, str(value), key)
                event = {
                    **field_record,
                    "action": "filled",
                    "success": True,
                    "mapped_value": value,
                    "source": value_payload.get("source"),
                    "confidence": value_payload.get("confidence"),
                    "estimated": value_payload.get("estimated"),
                    "reason": value_payload.get("reason"),
                }
            except Exception as exc:
                event = {
                    **field_record,
                    "action": "fill_failed",
                    "success": False,
                    "mapped_value": value,
                    "source": value_payload.get("source"),
                    "confidence": value_payload.get("confidence"),
                    "estimated": value_payload.get("estimated"),
                    "reason": str(exc),
                }
                if state["required"]:
                    missing.append(label)
            employment_events.append(event)
            fill_records.append(event)

        if field_records:
            unresolved = self._form_required_fields(modal, unresolved_only=True)
            validation_records.append(
                {
                    "captured_at": datetime.utcnow().isoformat(),
                    "missing_fields": list(dict.fromkeys(missing)),
                    "unresolved_required_fields": unresolved,
                    "validation_messages": self._validation_messages(modal),
                }
            )
            self._save_employment_history_debug(
                debug_dir,
                employment_records,
                mapping_records,
                fill_records,
                estimate_records,
                validation_records,
            )
        return list(dict.fromkeys(missing))

    def _employment_field_key(self, label: str, field) -> str | None:
        metadata = label.lower()
        try:
            metadata += " " + field.evaluate(
                """el => [
                    el.name,
                    el.id,
                    el.type,
                    el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'),
                    el.getAttribute('autocomplete')
                ].filter(Boolean).join(' ')"""
            ).lower()
        except Exception:
            pass
        options = self._field_options(field)
        has_year_options = any(re.fullmatch(r"(19|20)\d{2}", option.strip()) for option in options)
        has_month_options = any(
            option.strip().lower() in {
                "january",
                "february",
                "march",
                "april",
                "may",
                "june",
                "july",
                "august",
                "september",
                "october",
                "november",
                "december",
            }
            for option in options
        )
        if re.search(r"\b(company|employer|organization)\b", metadata):
            return "company_name"
        if re.search(r"\b(job title|title|role|position)\b", metadata):
            return "job_title"
        if re.search(r"total.*years.*(professional|work|career|experience)|years.*(professional|work|career).*experience", metadata):
            return "total_experience_years"
        if re.search(r"(additional|remaining).*months.*experience|months.*(professional|work|career).*experience", metadata):
            return "total_experience_months"
        if "from" in metadata and has_year_options:
            return "start_year"
        if "to" in metadata and has_year_options:
            return "end_year"
        if "from" in metadata and ("month" in metadata or has_month_options):
            return "start_month"
        if "from" in metadata and "year" in metadata:
            return "start_year"
        if "to" in metadata and ("month" in metadata or has_month_options):
            return "end_month"
        if "to" in metadata and "year" in metadata:
            return "end_year"
        if re.search(r"currently|current.*work|work.*here", metadata):
            return "currently_work_here"
        if re.search(r"\b(city|location)\b", metadata):
            return "location"
        if re.search(r"description|responsibilities|summary", metadata):
            return "description"
        return None

    def _employment_value_for_key(
        self,
        key: str,
        record: dict,
        candidate_profile: CandidateProfile | None,
        resume: ResumeProfile | None,
        mode: str,
        all_records: list[dict] | None = None,
    ) -> dict:
        value = record.get(key)
        if key in {"total_experience_years", "total_experience_months"}:
            total_months = self._total_employment_duration_months(all_records or [record])
            years = total_months // 12
            months = total_months % 12
            return {
                "value": years if key == "total_experience_years" else months,
                "source": "Employment History",
                "confidence": 80 if total_months else 0,
                "estimated": False,
                "reason": "Calculated total professional experience from extracted employment history durations.",
            }
        if key == "location":
            value = self._employment_location(record) or (
                candidate_profile.current_location if candidate_profile else None
            ) or (self._extract_location_from_resume(resume) if resume else None)
            return {
                "value": value,
                "source": "Employment History" if self._employment_location(record) else "Candidate Profile / Resume",
                "confidence": record.get("confidence") or 62,
                "estimated": False,
                "reason": "Mapped employment location with profile/resume fallback.",
            }
        if key in {"start_month", "end_month"} and not value and record.get(key.replace("month", "year")):
            if mode == "aggressive":
                return {
                    "value": "January",
                    "source": "Aggressive Employment Estimate",
                    "confidence": "Estimated / Low",
                    "estimated": True,
                    "reason": f"{key} was missing but year was available; estimated January.",
                }
            return {
                "value": None,
                "source": "Employment History",
                "confidence": "Low",
                "estimated": False,
                "reason": f"{key} missing and Safe Mode does not estimate dates.",
            }
        if key in {"end_month", "end_year"} and record.get("currently_work_here"):
            return {
                "value": None,
                "source": "Employment History",
                "confidence": record.get("confidence") or 80,
                "estimated": False,
                "reason": "Current employer; end date intentionally left blank.",
            }
        if key == "description" and not value and mode == "aggressive" and (record.get("job_title") or record.get("company_name")):
            generated = self._generated_employment_description(record)
            return {
                "value": generated,
                "source": "Aggressive Employment Estimate",
                "confidence": "Estimated / Low",
                "estimated": True,
                "reason": "Description missing; generated concise description from employment record.",
            }
        return {
            "value": value,
            "source": "Employment History",
            "confidence": record.get("confidence") or 0,
            "estimated": False,
            "reason": "Mapped from extracted resume employment history.",
        }

    def _employment_location(self, record: dict) -> str | None:
        parts = [record.get("city"), record.get("state"), record.get("country")]
        value = ", ".join(str(part).strip() for part in parts if part and str(part).strip())
        return value or None

    def _total_employment_duration_months(self, records: list[dict]) -> int:
        total = 0
        for record in records:
            try:
                months = int(record.get("duration_months") or 0)
            except (TypeError, ValueError):
                months = 0
            total += max(months, 0)
        return total

    def _generated_employment_description(self, record: dict) -> str:
        skills = ", ".join((record.get("skills_used") or record.get("technologies_used") or [])[:5])
        title = record.get("job_title") or "role"
        company = record.get("company_name") or "the organization"
        if skills:
            return self._trim_employment_description(
                f"Worked as {title} at {company}, contributing to responsibilities involving {skills}."
            )
        return self._trim_employment_description(f"Worked as {title} at {company}.")

    def _trim_employment_description(self, value: str, limit: int = 1000) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) <= limit:
            return value
        trimmed = value[:limit].rsplit(" ", 1)[0].strip()
        return trimmed or value[:limit]

    def _fill_employment_field(self, field, field_state: dict, value: str, key: str) -> None:
        if field_state["type"] in {"checkbox", "radio"}:
            if str(value).lower() in {"true", "1", "yes", "checked"}:
                try:
                    field.check(force=True)
                except Exception:
                    field.click(force=True)
            return
        if field_state["field_type"] == "select":
            options = self._field_options(field)
            choice = self._closest_option(value, options)
            field.select_option(label=choice)
            return
        field.fill(self._trim_employment_description(value) if key == "description" else value)
        if key == "location":
            try:
                field.press("ArrowDown", timeout=2000)
                field.press("Enter", timeout=2000)
            except Exception:
                try:
                    field.press("Enter", timeout=2000)
                except Exception:
                    pass

    def _save_employment_history_debug(
        self,
        debug_dir: Path | None,
        employment_records: list[dict],
        mapping_records: list[dict],
        fill_records: list[dict],
        estimate_records: list[dict],
        validation_records: list[dict],
    ) -> None:
        if debug_dir is None:
            return
        debug_dir.mkdir(parents=True, exist_ok=True)
        payloads = {
            "employment_records.json": employment_records,
            "employment_mapping.json": mapping_records,
            "employment_fields_filled.json": fill_records,
            "employment_estimates.json": estimate_records,
            "employment_validation.json": validation_records,
        }
        for filename, payload in payloads.items():
            (debug_dir / filename).write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )

    def _record_workflow_trace(
        self,
        page: Page,
        modal,
        debug_dir: Path | None,
        trace: list[dict],
        seen_states: dict[str, int],
        step_number: int,
        action_attempted: str,
        action_completed: bool,
        next_page_detected: str | None,
        retry_count: int,
        started_at: datetime,
        stop_reason: str | None = None,
    ) -> dict:
        state = self._workflow_state(page, modal)
        page_hash = state["page_hash"]
        if seen_states.get("__last_hash") == page_hash:
            repeated_count = int(seen_states.get("__repeat_count", 0)) + 1
        else:
            repeated_count = 1
        seen_states["__last_hash"] = page_hash
        seen_states["__repeat_count"] = repeated_count
        entry = {
            "step_number": step_number,
            "page_name": state["page_name"],
            "modal_title": state["modal_title"],
            "detected_page_type": state["detected_page_type"],
            "page_hash": page_hash,
            "repeated_count": repeated_count,
            "action_attempted": action_attempted,
            "action_completed": action_completed,
            "next_page_detected": next_page_detected,
            "current_url": state["current_url"],
            "visible_buttons": state["visible_buttons"],
            "enabled_buttons": state["enabled_buttons"],
            "unresolved_required_fields": state["unresolved_required_fields"],
            "validation_messages": state["validation_messages"],
            "retry_count": retry_count,
            "elapsed_time": round((datetime.utcnow() - started_at).total_seconds(), 2),
            "stop_reason": stop_reason,
        }
        if repeated_count >= 3 and stop_reason is None:
            entry["stop_reason"] = "Repeated workflow state detected."
        trace.append(entry)
        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "workflow_trace.json").write_text(
                json.dumps(trace, indent=2, default=str),
                encoding="utf-8",
            )
        return entry

    def _workflow_state(self, page: Page, modal) -> dict:
        modal_title = self._modal_title(modal) if modal is not None else ""
        visible_buttons = self._modal_buttons(modal) if modal is not None else []
        enabled_buttons = [button["text"] for button in visible_buttons if button.get("enabled")]
        unresolved = self._form_required_fields(modal, unresolved_only=True) if modal is not None else []
        validation = self._validation_messages(modal) if modal is not None else []
        required = self._form_required_fields(modal) if modal is not None else []
        resume_status = self._resume_upload_status(modal) if modal is not None else {}
        page_type = self._detect_workflow_page_type(modal, required, unresolved, validation, resume_status)
        signature = {
            "modal_title": modal_title,
            "page_type": page_type,
            "buttons": visible_buttons,
            "required": required,
            "unresolved": unresolved,
            "validation": validation,
            "resume": resume_status,
        }
        return {
            "page_name": self._safe_page_title(page),
            "modal_title": modal_title,
            "detected_page_type": page_type,
            "current_url": getattr(page, "url", ""),
            "visible_buttons": visible_buttons,
            "enabled_buttons": enabled_buttons,
            "unresolved_required_fields": unresolved,
            "validation_messages": validation,
            "page_hash": hashlib.sha256(
                json.dumps(signature, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        }

    def _detect_workflow_page_type(
        self,
        modal,
        required_fields: list[dict],
        unresolved_fields: list[dict],
        validation_messages: list[str],
        resume_status: dict,
    ) -> str:
        if modal is None:
            return "modal_closed"
        if validation_messages:
            return "validation_error"
        if resume_status.get("resume_upload_present"):
            return "resume_page"
        labels = " ".join(field.get("label", "") for field in required_fields).lower()
        if any(term in labels for term in ("first name", "last name", "phone", "email", "location")):
            return "contact_page"
        if any(term in labels for term in ("month of from", "month of to", "from", "to", "title", "company")):
            return "employment_history"
        if self._looks_like_screening_page(modal):
            return "screening_questions"
        buttons = {button["text"].lower() for button in self._modal_buttons(modal)}
        if "submit application" in buttons or "review" in buttons:
            return "review_page"
        if unresolved_fields:
            return "required_fields"
        return "unknown"

    def _navigation_failure_reason(self, before_state: dict, after_state: dict, button_text: str | None) -> str | None:
        if after_state["validation_messages"]:
            unresolved_labels = " ".join(
                field.get("label", "") for field in after_state.get("unresolved_required_fields") or []
            ).lower()
            if any(term in unresolved_labels for term in ("month of from", "year of from", "month of to", "year of to", "company", "job title")):
                return None
            return "Validation error: " + "; ".join(after_state["validation_messages"][:5])
        if before_state["page_hash"] == after_state["page_hash"]:
            page_type = after_state["detected_page_type"]
            unresolved = after_state["unresolved_required_fields"]
            if unresolved:
                labels = ", ".join(field["label"] for field in unresolved[:8])
                return f"{page_type.replace('_', ' ').title()} did not advance after clicking {button_text}; unresolved required fields remain: {labels}."
            return None
        before_unresolved = len(before_state["unresolved_required_fields"])
        after_unresolved = len(after_state["unresolved_required_fields"])
        if after_unresolved > before_unresolved:
            unresolved_labels = " ".join(
                field.get("label", "") for field in after_state.get("unresolved_required_fields") or []
            ).lower()
            if any(
                term in unresolved_labels
                for term in (
                    "month of from",
                    "year of from",
                    "month of to",
                    "year of to",
                    "company",
                    "job title",
                    "total years of professional experience",
                    "total additional months of experience",
                )
            ):
                return None
            labels = ", ".join(field["label"] for field in after_state["unresolved_required_fields"][:8])
            return f"Navigation revealed unresolved required fields: {labels}."
        return None

    def _step_limit_reason(self, modal) -> str:
        if modal is None:
            return "Navigation stopped because the LinkedIn modal closed before Review/Submit."
        validation = self._validation_messages(modal)
        if validation:
            return "Navigation stopped because validation prevented progress: " + "; ".join(validation[:5])
        unresolved = self._form_required_fields(modal, unresolved_only=True)
        if unresolved:
            return "Navigation stopped because required fields remained unresolved: " + ", ".join(
                field["label"] for field in unresolved[:8]
            )
        return "Navigation stopped because the Easy Apply workflow did not reach Review/Submit before the safety step limit."

    def _record_form_navigation_step(
        self,
        page: Page,
        modal,
        debug_dir: Path | None,
        steps: list[dict],
        step_number: int,
        action_taken: str,
        action_result: str,
        reason_for_stop: str | None,
    ) -> None:
        if not debug_dir:
            return
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / f"step_{step_number:02d}_{len(steps) + 1:02d}.png"
        try:
            try:
                page.screenshot(path=str(screenshot_path), full_page=True, timeout=5000)
            except TypeError:
                page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as exc:
            logger.warning("Could not capture form navigation screenshot: %s", exc)
            screenshot_path = None
        snapshot = self._form_navigation_snapshot(
            page,
            modal,
            step_number,
            str(screenshot_path) if screenshot_path else None,
            action_taken,
            action_result,
            reason_for_stop,
        )
        steps.append(snapshot)
        (debug_dir / "form_navigation_steps.json").write_text(
            json.dumps(steps, indent=2, default=str),
            encoding="utf-8",
        )

    def _form_navigation_snapshot(
        self,
        page: Page,
        modal,
        step_number: int,
        screenshot_path: str | None,
        action_taken: str,
        action_result: str,
        reason_for_stop: str | None,
    ) -> dict:
        if modal is None:
            return {
                "step_number": step_number,
                "screenshot_path": screenshot_path,
                "page_title": self._safe_page_title(page),
                "modal_title": "",
                "visible_buttons": [],
                "required_fields": [],
                "unresolved_required_fields": [],
                "filled_fields": [],
                "validation_messages": [],
                "selected_resume_status": {"resume_upload_present": False, "selected_resume_present": False},
                "navigation_controls": {},
                "action_taken": action_taken,
                "action_result": action_result,
                "reason_for_stop": reason_for_stop,
                "captured_at": datetime.utcnow().isoformat(),
            }
        return {
            "step_number": step_number,
            "screenshot_path": screenshot_path,
            "page_title": self._safe_page_title(page),
            "modal_title": self._modal_title(modal),
            "visible_buttons": self._modal_buttons(modal),
            "required_fields": self._form_required_fields(modal),
            "unresolved_required_fields": self._form_required_fields(modal, unresolved_only=True),
            "filled_fields": self._filled_fields(modal),
            "validation_messages": self._validation_messages(modal),
            "selected_resume_status": self._resume_upload_status(modal),
            "navigation_controls": self._navigation_control_summary(modal),
            "action_taken": action_taken,
            "action_result": action_result,
            "reason_for_stop": reason_for_stop,
            "captured_at": datetime.utcnow().isoformat(),
        }

    def _safe_page_title(self, page: Page) -> str:
        try:
            return page.title()
        except Exception:
            return ""

    def _modal_title(self, modal) -> str:
        for selector in ("h1:visible", "h2:visible", "h3:visible", "[role='heading']:visible"):
            locator = modal.locator(selector)
            if locator.count() > 0:
                text = locator.first.inner_text().strip()
                if text:
                    return text
        return ""

    def _modal_buttons(self, modal) -> list[dict]:
        buttons = modal.locator("button:visible")
        results: list[dict] = []
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                text = button.inner_text().strip()
                if text:
                    results.append({"text": text, "enabled": button.is_enabled()})
            except Exception:
                continue
        return results

    def _navigation_control_summary(self, modal) -> dict:
        summary = {}
        for text in ("Next", "Continue", "Review", "Submit application"):
            locator = modal.get_by_role("button", name=re.compile(rf"^\s*{re.escape(text)}\s*$", re.I))
            summary[text] = {
                "available": locator.count() > 0,
                "enabled": locator.count() > 0 and locator.first.is_enabled(),
            }
        buttons = modal.locator("button:visible")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                label = re.sub(r"\s+", " ", button.inner_text().strip())
            except Exception:
                continue
            for text in summary:
                if label.lower() == text.lower():
                    summary[text] = {
                        "available": True,
                        "enabled": button.is_enabled(),
                    }
        return summary

    def _form_required_fields(self, modal, unresolved_only: bool = False) -> list[dict]:
        fields = modal.locator("input[required]:visible, textarea[required]:visible, select[required]:visible")
        results: list[dict] = []
        for index in range(fields.count()):
            field = fields.nth(index)
            state = self._field_state(field)
            if unresolved_only and state["has_value"]:
                continue
            results.append(
                {
                    "label": self._field_label(field, index),
                    "field_type": state["field_type"],
                    "has_value": state["has_value"],
                }
            )
        return results

    def _filled_fields(self, modal) -> list[dict]:
        fields = modal.locator("input:visible, textarea:visible, select:visible")
        results: list[dict] = []
        for index in range(fields.count()):
            field = fields.nth(index)
            state = self._field_state(field)
            if state["has_value"]:
                results.append(
                    {
                        "label": self._field_label(field, index),
                        "field_type": state["field_type"],
                    }
                )
        return results[:30]

    def _validation_messages(self, modal) -> list[str]:
        selectors = (
            ".artdeco-inline-feedback--error:visible, "
            ".fb-dash-form-element__error-text:visible, "
            "[role='alert']:visible, "
            ".error:visible"
        )
        try:
            return [
                text.strip()
                for text in modal.locator(selectors).all_inner_texts()
                if text.strip()
            ]
        except Exception:
            return []

    def _resume_upload_status(self, modal) -> dict:
        upload_inputs = modal.locator("input[type='file']")
        text = ""
        try:
            text = modal.inner_text(timeout=1000)
        except Exception:
            pass
        detected = re.findall(r"[A-Za-z0-9][^\r\n]{2,}\.(?:pdf|docx?|txt)\b", text, re.I)
        detected_filename = detected[-1].strip() if detected else None
        return {
            "resume_upload_present": upload_inputs.count() > 0,
            "selected_resume_present": bool(detected_filename),
            "detected_filename": detected_filename,
        }

    def _upload_resume_if_needed(
        self,
        modal,
        resume_path: Path,
        diagnostics: dict | None = None,
        diagnostics_dir: Path | None = None,
    ) -> bool:
        status = self._resume_upload_status(modal)
        if diagnostics is not None:
            diagnostics["linkedin_filename_detected_after_upload"] = status.get("detected_filename")
            if diagnostics.get("upload_success") and not diagnostics.get("mismatch_detected"):
                self._save_resume_upload_diagnostics(diagnostics_dir, diagnostics)
                return False
        if not status["resume_upload_present"]:
            if diagnostics is not None:
                diagnostics["reason_if_failed"] = diagnostics.get("reason_if_failed") or "No resume upload input was present on this form step."
                self._save_resume_upload_diagnostics(diagnostics_dir, diagnostics)
            return False
        upload_inputs = modal.locator("input[type='file']")
        if status["selected_resume_present"] and upload_inputs.count() == 0:
            if diagnostics is not None:
                detected_filename = status.get("detected_filename")
                expected_filename = diagnostics.get("resume_original_filename")
                diagnostics["upload_completed"] = True
                diagnostics["upload_success"] = self._filenames_match(expected_filename, detected_filename)
                diagnostics["mismatch_detected"] = not diagnostics["upload_success"]
                diagnostics["reason_if_failed"] = (
                    None
                    if diagnostics["upload_success"]
                    else f"LinkedIn shows {detected_filename!r}, expected {expected_filename!r}."
                )
                self._save_resume_upload_diagnostics(diagnostics_dir, diagnostics)
            return False
        uploaded = False
        for index in range(upload_inputs.count()):
            if diagnostics is not None:
                diagnostics["upload_started"] = True
                diagnostics["file_passed_to_playwright"] = str(resume_path)
            upload_inputs.nth(index).set_input_files(
                self._resume_upload_payload(resume_path, diagnostics)
            )
            uploaded = True
        if diagnostics is not None:
            refreshed_status = self._resume_upload_status(modal)
            detected_filename = refreshed_status.get("detected_filename")
            expected_filename = diagnostics.get("resume_original_filename")
            diagnostics["linkedin_filename_detected_after_upload"] = detected_filename
            diagnostics["upload_completed"] = uploaded
            diagnostics["upload_success"] = bool(uploaded) and (
                detected_filename is None or self._filenames_match(expected_filename, detected_filename)
            )
            diagnostics["mismatch_detected"] = bool(detected_filename) and not self._filenames_match(expected_filename, detected_filename)
            diagnostics["reason_if_failed"] = (
                None
                if diagnostics["upload_success"] and not diagnostics["mismatch_detected"]
                else f"LinkedIn shows {detected_filename!r}, expected {expected_filename!r}."
            )
            self._save_resume_upload_diagnostics(diagnostics_dir, diagnostics)
        return uploaded

    def _create_resume_upload_diagnostics(self, job: JobOpportunity, resume: ResumeProfile) -> tuple[Path, dict]:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        linkedin_job_id = self._linkedin_job_id(job.job_url) or str(job.id)
        safe_title = re.sub(r"[^A-Za-z0-9_.-]+", "_", job.title or "job").strip("_")[:80]
        debug_dir = self.settings.resume_upload_debug_dir / timestamp / f"{linkedin_job_id}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        raw_path = Path(resume.stored_path) if resume.stored_path else Path()
        storage_path = raw_path.resolve() if resume.stored_path else raw_path
        file_exists = bool(resume.stored_path) and storage_path.exists() and storage_path.is_file()
        mismatch = job.resume_id != resume.id
        reason = None
        if mismatch:
            reason = f"Job resume_id {job.resume_id} does not match active resume_id {resume.id}."
        elif not file_exists:
            reason = f"Resume file for resume_id {resume.id} is missing: {storage_path}."
        diagnostics = {
            "active_resume_id": resume.id,
            "job_resume_id": job.resume_id,
            "search_batch_id": job.search_batch_id,
            "resume_filename_from_database": resume.filename,
            "resume_original_filename": resume.filename,
            "resume_storage_path": str(storage_path),
            "file_exists": file_exists,
            "file_size": storage_path.stat().st_size if file_exists else None,
            "file_hash_sha256": self._sha256(storage_path) if file_exists else None,
            "file_passed_to_playwright": str(storage_path) if file_exists and not mismatch else None,
            "linkedin_filename_detected_after_upload": None,
            "upload_started": False,
            "upload_completed": False,
            "upload_success": False,
            "mismatch_detected": mismatch,
            "reason_if_failed": reason,
            "evidence_path": str(debug_dir),
        }
        self._save_resume_upload_diagnostics(debug_dir, diagnostics)
        return debug_dir, diagnostics

    def _save_resume_upload_diagnostics(self, debug_dir: Path | None, diagnostics: dict | None) -> None:
        if debug_dir is None or diagnostics is None:
            return
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "resume_upload_check.json").write_text(
            json.dumps(diagnostics, indent=2, default=str),
            encoding="utf-8",
        )

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _filenames_match(self, expected: str | None, detected: str | None) -> bool:
        if not expected or not detected:
            return False
        return Path(expected).name.strip().lower() == Path(detected).name.strip().lower()

    def _resume_upload_payload(self, resume_path: Path, diagnostics: dict | None):
        if diagnostics is None:
            return str(resume_path)
        original_filename = diagnostics.get("resume_original_filename") or resume_path.name
        mime_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
        return {
            "name": Path(original_filename).name,
            "mimeType": mime_type,
            "buffer": resume_path.read_bytes(),
        }

    def _looks_like_screening_page(self, modal) -> bool:
        try:
            text = modal.inner_text(timeout=1000).lower()
        except Exception:
            return False
        return any(term in text for term in ("screening", "question", "authorization", "sponsorship", "years of experience"))

    def _fill_contact_fields(
        self,
        modal,
        candidate_profile: CandidateProfile | None,
        resume: ResumeProfile | None,
        contact_debug_dir: Path | None,
        profile_apply_debug_dir: Path | None,
        contact_events: list[dict],
    ) -> list[str]:
        fields = modal.locator("input:visible, textarea:visible, select:visible")
        missing: list[str] = []
        for index in range(fields.count()):
            field = fields.nth(index)
            label = self._field_label(field, index)
            contact_key = self._contact_field_key(label, field)
            if contact_key is None:
                continue
            field_state = self._field_state(field)
            event = {
                "time": datetime.utcnow().isoformat(),
                "field_label": label,
                "contact_key": contact_key,
                "field_type": field_state["field_type"],
                "required": field_state["required"],
                "existing_value_present": field_state["has_value"],
                "value_source": None,
                "value_found": None,
                "reason": None,
                "action": "detected",
                "success": None,
                "error": None,
            }
            if field_state["has_value"]:
                event["action"] = "kept_existing_value"
                event["success"] = True
                event["reason"] = "LinkedIn Easy Apply already had a value; existing value was preserved."
                contact_events.append(event)
                continue

            value, value_source, confidence = self._candidate_contact_value(candidate_profile, resume, contact_key)
            if contact_key == "current_location":
                modal_location = self._extract_location_from_modal(modal)
                if modal_location and (not value or value_source == "Resume Visible Text"):
                    value, value_source, confidence = modal_location, "LinkedIn Easy Apply Header", 85
            if not value:
                event["action"] = "missing_candidate_profile_value"
                event["success"] = False
                event["reason"] = self._missing_contact_reason(contact_key, candidate_profile, resume)
                contact_events.append(event)
                if field_state["required"]:
                    missing.append(label)
                continue

            event["value_source"] = value_source
            event["value_found"] = value
            event["confidence"] = confidence
            try:
                fill_action = self._fill_contact_field(field, field_state, value, contact_key)
                event["action"] = fill_action
                event["success"] = True
                event["reason"] = (
                    f"Filled from {value_source}."
                    if fill_action == "filled"
                    else f"Filled from {value_source} and selected a LinkedIn typeahead suggestion."
                )
            except Exception as exc:
                event["action"] = "fill_failed"
                event["success"] = False
                event["error"] = str(exc)
                event["reason"] = f"Value was available from {value_source}, but filling failed."
                if field_state["required"]:
                    missing.append(label)
            contact_events.append(event)

        self._save_contact_debug(contact_debug_dir, contact_events)
        self._save_easy_apply_profile_debug(profile_apply_debug_dir, contact_events)
        return list(dict.fromkeys(missing))

    def _contact_field_key(self, label: str, field) -> str | None:
        metadata = label.lower()
        try:
            metadata += " " + field.evaluate(
                """el => [
                    el.name,
                    el.id,
                    el.type,
                    el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'),
                    el.getAttribute('autocomplete')
                ].filter(Boolean).join(' ')"""
            ).lower()
        except Exception:
            pass
        if "first" in metadata and "name" in metadata:
            return "first_name"
        if "last" in metadata and "name" in metadata:
            return "last_name"
        if "email" in metadata:
            return "email"
        if "country" in metadata and ("code" in metadata or "phone" in metadata):
            return "phone_country_code"
        if "phone" in metadata or "mobile" in metadata:
            return "mobile_phone"
        if any(term in metadata for term in ("city", "location", "current address")):
            return "current_location"
        if "linkedin" in metadata:
            return "linkedin_url"
        if "portfolio" in metadata or "website" in metadata:
            return "portfolio_url"
        if "github" in metadata:
            return "github_url"
        return None

    def _candidate_contact_value(
        self,
        candidate_profile: CandidateProfile | None,
        resume: ResumeProfile | None,
        contact_key: str,
    ) -> tuple[str | None, str | None, float | None]:
        if candidate_profile is not None:
            value = getattr(candidate_profile, contact_key, None)
            if value is not None and str(value).strip():
                metadata = (candidate_profile.field_sources or {}).get(contact_key, {})
                return (
                    str(value).strip(),
                    metadata.get("source") or "Candidate Profile",
                    metadata.get("confidence") or 100,
                )
        if contact_key == "current_location" and resume is not None:
            location = self._extract_location_from_resume(resume)
            if location:
                return location, "Resume Visible Text", 62
        return None, None, None

    def _missing_contact_reason(
        self,
        contact_key: str,
        candidate_profile: CandidateProfile | None,
        resume: ResumeProfile | None,
    ) -> str:
        if contact_key == "current_location":
            profile_location = candidate_profile.current_location if candidate_profile else None
            resume_location = self._extract_location_from_resume(resume) if resume else None
            if not profile_location and not resume_location:
                return "No city/location was available in Candidate Profile or resume extraction."
            return "A location candidate existed but could not be used."
        return "No value was available in Candidate Profile or supported fallbacks."

    def _extract_location_from_modal(self, modal) -> str | None:
        try:
            text = modal.inner_text(timeout=1000)
        except Exception:
            return None
        location_pattern = re.compile(
            r"\b([A-Z][A-Za-z .'-]+,\s*(?:[A-Z][A-Za-z .'-]+,\s*)?(?:India|United States|USA|Canada|United Kingdom|UK|Germany|France|Australia))\b"
        )
        for line in text.splitlines():
            cleaned = re.sub(r"\s+", " ", line).strip()
            if "@" in cleaned or len(cleaned) > 90:
                continue
            match = location_pattern.search(cleaned)
            if match:
                return match.group(1).strip()
        match = location_pattern.search(text)
        return match.group(1).strip() if match else None

    def _field_state(self, field) -> dict:
        try:
            return field.evaluate(
                """el => {
                    const tag = el.tagName.toLowerCase();
                    const type = (el.type || tag).toLowerCase();
                    let value = '';
                    if (type === 'radio') {
                        const roots = [
                            el.closest('fieldset'),
                            el.closest('.fb-dash-form-element'),
                            el.closest('[data-test-form-element]'),
                            el.closest('[data-test-form-builder-question]'),
                            el.closest('.jobs-easy-apply-form-section__grouping'),
                            el.closest('.jobs-easy-apply-form-section'),
                            el.closest('[role="group"]')
                        ].filter(Boolean);
                        let group = roots.find(root => root.querySelectorAll('input[type="radio"]').length >= 2);
                        if (!group) {
                            let parent = el.parentElement;
                            for (let i = 0; parent && i < 8; i += 1) {
                                const count = parent.querySelectorAll('input[type="radio"]').length;
                                if (count >= 2 && count <= 8) {
                                    group = parent;
                                    break;
                                }
                                parent = parent.parentElement;
                            }
                        }
                        const name = el.getAttribute('name');
                        const sameName = name && group
                            ? Array.from(group.querySelectorAll('input[type="radio"]')).filter(input => input.getAttribute('name') === name)
                            : [];
                        const controls = sameName.length
                            ? sameName
                            : Array.from(group?.querySelectorAll('input[type="radio"]') || [el]);
                        const checked = controls.find(input => input.checked || input.getAttribute('aria-checked') === 'true');
                        value = checked ? (checked.value || 'checked') : '';
                    } else if (type === 'checkbox') {
                        value = el.checked ? (el.value || 'checked') : '';
                    } else if (tag === 'select') {
                        const selected = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
                        const selectedText = selected ? String(selected.textContent || '').trim() : '';
                        value = el.value || '';
                        const placeholderPattern = /^(|select an option|please select|choose an option|select)$/i;
                        if (el.selectedIndex <= 0 && placeholderPattern.test(selectedText || String(value).trim())) {
                            value = '';
                        }
                    } else {
                        value = el.value || '';
                    }
                    const describedIds = (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
                    const hintText = describedIds.map(id => (document.getElementById(id)?.innerText || '')).join(' ');
                    const container = el.closest('.fb-dash-form-element, [data-test-form-element], .artdeco-text-input, .jobs-easy-apply-form-section__grouping') || el.parentElement;
                    const containerText = container ? String(container.innerText || '') : '';
                    const numericHint = /whole number|between 0 and 99|enter a number|numeric|must be a number/i.test(hintText + ' ' + containerText);
                    const numeric = type === 'number'
                        || (el.getAttribute('inputmode') || '').toLowerCase() === 'numeric'
                        || /^\\(?\\[?0-9/.test(el.getAttribute('pattern') || '')
                        || numericHint;
                    return {
                        tag,
                        type,
                        field_type: tag === 'select' ? 'select' : type,
                        numeric: Boolean(numeric),
                        required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
                        has_value: Boolean(String(value).trim())
                    };
                }"""
            )
        except Exception:
            return {
                "tag": "input",
                "type": "text",
                "field_type": "input",
                "numeric": False,
                "required": False,
                "has_value": False,
            }

    def _fill_contact_field(self, field, field_state: dict, value: str, contact_key: str | None = None) -> str:
        if field_state["field_type"] == "select":
            try:
                field.select_option(label=value)
                return "filled"
            except Exception:
                field.select_option(value=value)
                return "filled"
        field.fill(value)
        if contact_key == "current_location":
            try:
                field.press("ArrowDown", timeout=2000)
                field.press("Enter", timeout=2000)
                return "filled_typeahead"
            except Exception:
                try:
                    field.press("Enter", timeout=2000)
                    return "filled_typeahead"
                except Exception:
                    return "filled"
        return "filled"

    def _save_contact_debug(
        self,
        contact_debug_dir: Path | None,
        contact_events: list[dict],
    ) -> None:
        if not contact_debug_dir:
            return
        contact_debug_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "captured_at": datetime.utcnow().isoformat(),
            "events": contact_events,
            "contact_fields_detected": self._contact_detected(contact_events),
            "contact_fields_filled": self._contact_filled(contact_events),
            "missing_contact_fields": self._contact_missing(contact_events),
        }
        (contact_debug_dir / "contact_fields.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _save_easy_apply_profile_debug(
        self,
        profile_apply_debug_dir: Path | None,
        contact_events: list[dict],
    ) -> None:
        if not profile_apply_debug_dir:
            return
        prefilled = [
            event
            for event in contact_events
            if event["action"] == "kept_existing_value"
        ]
        missing = [
            event
            for event in contact_events
            if event["required"]
            and event["action"] in {"missing_candidate_profile_value", "fill_failed"}
        ]
        self._save_profile_apply_debug(
            profile_apply_debug_dir,
            "easy_apply_prefilled_fields.json",
            {
                "captured_at": datetime.utcnow().isoformat(),
                "fields": prefilled,
            },
        )
        self._save_profile_apply_debug(
            profile_apply_debug_dir,
            "missing_required_fields.json",
            {
                "captured_at": datetime.utcnow().isoformat(),
                "fields": missing,
                "field_names": [event["field_label"] for event in missing],
            },
        )

    def _contact_detected(self, contact_events: list[dict]) -> list[dict]:
        return [
            {
                "field_label": event["field_label"],
                "contact_key": event["contact_key"],
                "field_type": event["field_type"],
                "required": event["required"],
                "existing_value_present": event["existing_value_present"],
            }
            for event in contact_events
        ]

    def _contact_filled(self, contact_events: list[dict]) -> list[dict]:
        return [
            {
                "field_label": event["field_label"],
                "contact_key": event["contact_key"],
                "value_source": event["value_source"],
            }
            for event in contact_events
            if event["action"] in {"filled", "filled_typeahead"} and event["success"]
        ]

    def _contact_missing(self, contact_events: list[dict]) -> list[str]:
        return list(
            dict.fromkeys(
                event["field_label"]
                for event in contact_events
                if event["action"] in {"missing_candidate_profile_value", "fill_failed"}
                and event["required"]
            )
        )

    def _detect_unanswered_custom_questions(self, modal) -> str | None:
        required_inputs = modal.locator(
            "input[required]:visible, textarea[required]:visible, select[required]:visible"
        )
        unanswered: list[str] = []
        for index in range(required_inputs.count()):
            field = required_inputs.nth(index)
            if not self._field_state(field)["has_value"]:
                unanswered.append(self._field_label(field, index))
        if unanswered:
            labels = ", ".join(unanswered[:8])
            return f"Required questions need review: {labels}."
        return None

    def _answer_screening_questions(
        self,
        page,
        modal,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
        debug_dir: Path | None,
        screening_events: list[dict],
    ) -> list[dict]:
        mode = (screening_context.get("mode") or "safe").lower()
        fields = modal.locator("input[required]:visible, textarea[required]:visible, select[required]:visible")
        selected: list[dict] = []
        detected: list[dict] = []
        field_traces: list[dict] = []
        radio_debug_dir: Path | None = None
        for index in range(fields.count()):
            field = fields.nth(index)
            state = self._field_state(field)
            if state["has_value"]:
                continue
            question = self._screening_question_text(field, index)
            category = self._screening_question_category(question)
            normalized_field = self._normalized_screening_field(question, category)
            detected_control_type = self._screening_control_type(state)
            if self._employment_field_key(question, field) and not self._should_screening_handle_field(question, category):
                field_traces.append(
                    {
                        "original_label": question,
                        "normalized_field": normalized_field,
                        "detected_control_type": detected_control_type,
                        "category": category,
                        "decision_pipeline_stage_used": None,
                        "selected_value": None,
                        "written_successfully": False,
                        "reason_if_left_blank": "Handled by employment-history autofill, not screening.",
                    }
                )
                continue
            options = self._field_options(field)
            detected.append(
                {
                    "question_text": question,
                    "original_label": question,
                    "normalized_field": normalized_field,
                    "category": category,
                    "field_type": state["field_type"],
                    "detected_control_type": detected_control_type,
                    "options": options,
                }
            )
            answer = self._select_screening_answer(
                question,
                category,
                state,
                options,
                resume,
                job,
                candidate_profile,
                screening_context,
            )
            if answer is None:
                blank_reason = self._screening_blank_reason(question, category, mode)
                event = {
                    "question_text": question,
                    "original_label": question,
                    "normalized_field": normalized_field,
                    "category": category,
                    "field_type": state["field_type"],
                    "detected_control_type": detected_control_type,
                    "options": options,
                    "selected_answer": None,
                    "source": None,
                    "confidence": "None",
                    "reason": blank_reason,
                    "mode": mode.title(),
                    "aggressive_default_used": False,
                    "filled_successfully": False,
                    "decision_stage": None,
                }
                screening_events.append(event)
                field_traces.append(self._screening_field_trace(event, blank_reason))
                continue
            safe_value = self._typesafe_answer_value(
                answer["answer"], state, options, question, resume, candidate_profile, screening_context
            )
            if safe_value is None:
                # The computed answer was the wrong type for this control (e.g. a
                # yes/no answer for a free-text box because the label mis-resolved).
                # Leave it blank instead of writing an invalid value.
                blank_reason = "Answer type did not match the field; left blank to avoid an invalid value."
                event = {
                    "question_text": question,
                    "original_label": question,
                    "normalized_field": normalized_field,
                    "category": category,
                    "field_type": state["field_type"],
                    "detected_control_type": detected_control_type,
                    "options": options,
                    "selected_answer": None,
                    "source": None,
                    "confidence": "None",
                    "reason": blank_reason,
                    "mode": mode.title(),
                    "aggressive_default_used": False,
                    "filled_successfully": False,
                    "decision_stage": answer.get("decision_stage"),
                }
                screening_events.append(event)
                field_traces.append(self._screening_field_trace(event, blank_reason))
                continue
            answer["answer"] = safe_value
            answer["selected_answer"] = safe_value
            try:
                radio_writer = None
                if state.get("type") in {"radio", "checkbox"} and radio_debug_dir is None:
                    radio_debug_dir = self._create_radio_writer_debug_dir(job)
                radio_writer = self._fill_screening_field(
                    field,
                    state,
                    answer["answer"],
                    options,
                    page=page,
                    job=job,
                    question=question,
                    normalized_field=normalized_field,
                    radio_debug_dir=radio_debug_dir,
                )
                if radio_writer:
                    answer["radio_writer"] = radio_writer
                answer["filled_successfully"] = True
                write_reason = None
            except Exception as exc:
                answer["filled_successfully"] = False
                answer["error"] = str(exc)
                write_reason = f"Write failed: {exc}"
            answer.update(
                {
                    "question_text": question,
                    "original_label": question,
                    "normalized_field": normalized_field,
                    "category": category,
                    "field_type": state["field_type"],
                    "detected_control_type": detected_control_type,
                    "options": options,
                    "mode": mode.title(),
                }
            )
            screening_events.append(answer)
            field_traces.append(self._screening_field_trace(answer, write_reason))
            selected.append(answer)
        self._save_screening_debug(debug_dir, detected, screening_events, field_traces)
        self._save_reasoning_debug(job, resume, screening_events)
        return selected

    def _typesafe_answer_value(
        self,
        raw_value,
        state: dict,
        options: list[str],
        question: str,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
    ):
        """Ensure the answer's type matches the control before it is written.

        Prevents the LinkedIn failure where a numeric "years of experience" field
        (a plain text input with 0-99 validation) receives a "Yes"/"No" because its
        label mis-resolved to a neighbouring yes/no question. Choice controls are
        left untouched; free-text/number inputs never receive a yes/no answer, and
        numeric fields always receive digits (else the field is left blank).
        """
        field_type = str(state.get("field_type") or "").lower()
        control = str(state.get("type") or "").lower()
        if control in {"radio", "checkbox"} or field_type == "select":
            return raw_value
        value = str(raw_value).strip()
        numeric_field = self._looks_numeric_screening_field(state, question)
        # A yes/no answer must never be typed into a free-text/number input.
        if not options and value.lower() in {"yes", "no"}:
            if numeric_field:
                return self._numeric_fallback_answer(question, resume, candidate_profile, screening_context)
            return None
        # A numeric field must receive a whole number.
        if numeric_field and not re.fullmatch(r"-?\d+", value):
            return self._numeric_fallback_answer(question, resume, candidate_profile, screening_context)
        return raw_value

    def _looks_numeric_screening_field(self, state: dict, question: str) -> bool:
        if state.get("numeric"):
            return True
        if self._screening_question_category(question) in {"skill_years", "years"}:
            return True
        return bool(re.search(r"how many|number of|years of|\byears\b", question or "", re.I))

    def _numeric_fallback_answer(
        self,
        question: str,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
    ) -> str:
        """Best-effort whole-number answer for a numeric field: a skill-years
        estimate when the skill is known, otherwise an honest 0."""
        skill = self.screening.extract_skill_from_experience_question(question)
        if skill:
            estimate = self.screening.estimate_skill_years(skill, resume, candidate_profile)
            years = estimate.get("years")
            if estimate.get("found") and years is not None:
                return str(int(years))
        return "0"

    def _should_screening_handle_field(self, question: str, category: str) -> bool:
        if category in {
            "skill_years",
            "salary",
            "authorization",
            "work_preference",
            "clearance",
            "legal",
            "certification",
            "notice",
            "education",
            "skill",
        }:
            return True
        text = question.lower()
        return bool(re.search(r"\b(bachelor|master|degree|graduation year|green card|citizen|relocat\w*|commut\w*|hybrid|remote|onsite|on-site|unpaid training)\b", text))

    def _screening_control_type(self, field_state: dict) -> str:
        field_type = str(field_state.get("field_type") or "").lower()
        input_type = str(field_state.get("type") or "").lower()
        if field_type == "select":
            return "dropdown"
        if input_type in {"radio", "checkbox"}:
            return input_type
        if input_type in {"number", "tel"}:
            return "numeric_input"
        if field_type == "textarea":
            return "textarea"
        return "textbox"

    def _normalized_screening_field(self, question: str, category: str) -> str:
        if category == "skill_years":
            skill = self._extract_skill_from_experience_question(question)
            return f"{skill or 'Skill'} Years"
        if category == "graduation_year":
            return "Graduation Year"
        if category == "authorization":
            lowered = question.lower()
            if re.search(r"sponsor", lowered):
                return "Visa Sponsorship"
            if re.search(r"green card|citizen|authorized|authorization|right to work|work permit|eligible to work", lowered):
                return "US Work Authorization"
            return "Work Authorization"
        if category == "work_preference":
            lowered = question.lower()
            if re.search(r"relocat", lowered):
                return "Relocation Willingness"
            if re.search(r"commut", lowered):
                return "Commute Willingness"
            if re.search(r"hybrid|remote|onsite|on-site|office|work setting|work mode|work arrangement", lowered):
                return "Work Mode Preference"
            return "Candidate Work Preference"
        if category == "education":
            return "Education Requirement"
        if category == "location":
            return "Location"
        if category == "salary":
            return "Salary"
        return re.sub(r"\s+", " ", question).strip()[:120] or category.replace("_", " ").title()

    def _screening_blank_reason(self, question: str, category: str, mode: str) -> str:
        if category in {"authorization", "legal"}:
            return "Protected/compliance-sensitive question; requires saved candidate answer or recruiter review."
        if category in {"clearance", "certification"}:
            return "Credential question requires saved candidate answer, explicit Candidate Profile evidence, or recruiter review."
        if category == "work_preference":
            return "Candidate preference question requires saved candidate answer or recruiter review."
        if mode != "aggressive":
            return "Safe Mode did not have a saved or high-confidence answer."
        return "No supported evidence or fallback produced an answer."

    def _screening_field_trace(self, event: dict, reason_if_left_blank: str | None = None) -> dict:
        return {
            "original_label": event.get("original_label") or event.get("question_text"),
            "normalized_field": event.get("normalized_field"),
            "detected_control_type": event.get("detected_control_type") or event.get("field_type"),
            "category": event.get("category"),
            "decision_pipeline_stage_used": event.get("decision_stage"),
            "selected_value": event.get("selected_answer"),
            "written_successfully": bool(event.get("filled_successfully")),
            "reason_if_left_blank": None if event.get("filled_successfully") else (reason_if_left_blank or event.get("reason")),
            "source": event.get("source"),
            "confidence": event.get("confidence"),
            "decision_pipeline": event.get("decision_pipeline") or [],
            "radio_writer": event.get("radio_writer"),
        }

    def _select_screening_answer(
        self,
        question: str,
        category: str,
        field_state: dict,
        options: list[str],
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
    ) -> dict | None:
        # Decision logic now lives in the portal-agnostic ScreeningAnswerEngine.
        decision = self.screening.decide(
            ScreeningQuestion(
                text=question,
                category=category,
                field_type=field_state.get("type"),
                field_kind=field_state.get("field_type"),
                options=options,
                portal="linkedin",
            ),
            resume=resume,
            job=job,
            candidate_profile=candidate_profile,
            context=screening_context,
        )
        return decision.to_event()

    def _screening_question_category(self, question: str) -> str:
        return self.screening.categorize(question)

    def _screening_question_text(self, field, index: int) -> str:
        try:
            text = field.evaluate(
                """(el, fallback) => {
                    const id = el.id;
                    const byFor = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                    const wrapping = el.closest('label');
                    const fieldElement = el.closest('.fb-dash-form-element, .artdeco-text-input');
                    const fieldLabel = fieldElement?.querySelector(':scope > label, :scope > .artdeco-text-input--label, :scope > .fb-dash-form-element__label');
                    const type = (el.type || '').toLowerCase();
                    const questionRoot = el.closest('fieldset, .fb-dash-form-element, [data-test-form-element], [data-test-form-builder-question], .jobs-easy-apply-form-section__grouping, .jobs-easy-apply-form-section, [role="group"]');
                    const rootLabel = questionRoot?.querySelector(':scope > legend, :scope > label, :scope > .fb-dash-form-element__label, :scope > .jobs-easy-apply-form-section__label, [data-test-form-builder-question-label]');
                    let previous = el.previousElementSibling;
                    let previousLabel = '';
                    for (let i = 0; previous && i < 5; i += 1) {
                        if (previous.matches?.('label, .artdeco-text-input--label, .fb-dash-form-element__label')) {
                            previousLabel = previous.innerText || '';
                            break;
                        }
                        previous = previous.previousElementSibling;
                    }
                    const fieldset = el.closest('fieldset');
                    const legend = fieldset?.querySelector(':scope > legend');
                    if (type === 'radio' || type === 'checkbox') {
                        return (
                            legend?.innerText ||
                            rootLabel?.innerText ||
                            fieldLabel?.innerText ||
                            questionRoot?.getAttribute('aria-label') ||
                            el.getAttribute('name') ||
                            fallback
                        ).trim();
                    }
                    return (
                        byFor?.innerText ||
                        wrapping?.innerText ||
                        fieldLabel?.innerText ||
                        previousLabel ||
                        legend?.innerText ||
                        el.getAttribute('aria-label') ||
                        el.getAttribute('name') ||
                        el.getAttribute('placeholder') ||
                        fallback
                    ).trim();
                }""",
                f"required field {index + 1}",
            )
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            return self._field_label(field, index)

    def _field_options(self, field) -> list[str]:
        try:
            return [
                option.strip()
                for option in field.evaluate(
                    """el => {
                        if (el.tagName.toLowerCase() === 'select') {
                            return Array.from(el.options).map(option => option.textContent || option.value || '');
                        }
                        const roots = [
                            el.closest('fieldset'),
                            el.closest('.fb-dash-form-element'),
                            el.closest('[data-test-form-element]'),
                            el.closest('[data-test-form-builder-question]'),
                            el.closest('.jobs-easy-apply-form-section__grouping'),
                            el.closest('.jobs-easy-apply-form-section'),
                            el.closest('[role="group"]')
                        ].filter(Boolean);
                        let group = roots.find(root => root.querySelectorAll('input[type="radio"], input[type="checkbox"]').length >= 2);
                        if (!group) {
                            let parent = el.parentElement;
                            for (let i = 0; parent && i < 8; i += 1) {
                                const count = parent.querySelectorAll('input[type="radio"], input[type="checkbox"]').length;
                                if (count >= 2 && count <= 8) {
                                    group = parent;
                                    break;
                                }
                                parent = parent.parentElement;
                            }
                        }
                        const name = el.getAttribute('name');
                        const controls = Array.from(group?.querySelectorAll('input[type="radio"], input[type="checkbox"]') || []);
                        const scopedControls = name
                            ? controls.filter(input => input.getAttribute('name') === name)
                            : controls;
                        return (scopedControls.length >= 2 ? scopedControls : controls)
                            .map(input => {
                                const id = input.id;
                                const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                                const wrapper = input.closest('label, .fb-radio, .artdeco-radio, .jobs-easy-apply-form-section__option, li');
                                return label?.innerText || wrapper?.innerText || input.value || '';
                            });
                    }"""
                )
                if option and not re.match(r"^\s*select an option\s*$", option, re.I)
            ]
        except Exception:
            return []

    def _fill_screening_field(
        self,
        field,
        field_state: dict,
        answer: str,
        options: list[str],
        page=None,
        job: JobOpportunity | None = None,
        question: str | None = None,
        normalized_field: str | None = None,
        radio_debug_dir: Path | None = None,
    ) -> dict | None:
        if field_state["field_type"] == "select":
            choice = self._closest_option(answer, options)
            field.select_option(label=choice)
            return None
        if field_state["type"] in {"radio", "checkbox"}:
            result = self._click_choice_control(
                field,
                answer,
                options,
                page=page,
                job=job,
                question=question,
                normalized_field=normalized_field,
                radio_debug_dir=radio_debug_dir,
            )
            if not result.get("success"):
                reason = result.get("reason") or "LinkedIn did not accept the selected radio option."
                raise RuntimeError(reason)
            return result
        field.fill(str(answer))
        return None

    def _choice_control_writer_script(self) -> str:
        return """
            async (el, payload) => {
                /* radioWriterPayload */
                const normalize = value => String(value || '')
                    .replace(/\\s+/g, ' ')
                    .replace(/[’']/g, "'")
                    .trim()
                    .toLowerCase();
                const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
                const cssEscape = value => window.CSS && CSS.escape ? CSS.escape(value) : String(value || '').replace(/"/g, '\\"');
                const targetText = normalize(payload.answer);
                const fieldType = (el.type || '').toLowerCase();
                const controlSelector = 'input[type="radio"], input[type="checkbox"]';
                const optionSelector = 'label, .fb-radio, .artdeco-radio, .jobs-easy-apply-form-section__option, li, [data-test-form-builder-radio-button], [data-test-form-builder-checkbox], div';
                const rootSelectors = [
                    'fieldset',
                    '.fb-dash-form-element',
                    '[data-test-form-element]',
                    '[data-test-form-builder-question]',
                    '.jobs-easy-apply-form-section__grouping',
                    '.jobs-easy-apply-form-section',
                    '.artdeco-card',
                    '[role="group"]'
                ];
                const elementVisible = node => {
                    if (!node || !(node instanceof Element)) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width >= 0
                        && rect.height >= 0;
                };
                const controlsIn = root => Array.from(root?.querySelectorAll(controlSelector) || [])
                    .filter(input => ['radio', 'checkbox'].includes((input.type || '').toLowerCase()));
                const optionContainerFor = input => (
                    input.closest(optionSelector) || input.parentElement || input
                );
                const labelElementFor = input => {
                    const id = input.id;
                    const byFor = id ? document.querySelector(`label[for="${cssEscape(id)}"]`) : null;
                    return byFor || input.closest('label');
                };
                const labelFor = input => {
                    const id = input.id;
                    const byFor = id ? document.querySelector(`label[for="${cssEscape(id)}"]`) : null;
                    const labelledBy = input.getAttribute('aria-labelledby');
                    const ariaLabel = labelledBy
                        ? labelledBy.split(/\\s+/).map(idPart => document.getElementById(idPart)?.innerText || '').join(' ')
                        : '';
                    const closestLabel = input.closest('label');
                    const optionContainer = optionContainerFor(input);
                    return byFor?.innerText
                        || closestLabel?.innerText
                        || ariaLabel
                        || input.getAttribute('aria-label')
                        || optionContainer?.innerText
                        || input.value
                        || '';
                };
                const questionTextFor = root => {
                    const legend = root?.matches?.('fieldset') ? root.querySelector(':scope > legend') : null;
                    const directLabel = root?.querySelector?.(':scope > label, :scope > .fb-dash-form-element__label, :scope > .artdeco-text-input--label');
                    const heading = root?.querySelector?.('legend, .fb-dash-form-element__label, .jobs-easy-apply-form-section__label, [data-test-form-builder-question-label]');
                    return String(legend?.innerText || directLabel?.innerText || heading?.innerText || payload.question || '').replace(/\\s+/g, ' ').trim();
                };
                const candidateRoots = [];
                for (const selector of rootSelectors) {
                    const root = el.closest(selector);
                    if (root && !candidateRoots.includes(root)) candidateRoots.push(root);
                }
                let parent = el.parentElement;
                for (let i = 0; parent && i < 8; i += 1) {
                    const count = controlsIn(parent).length;
                    if (count >= 2 && count <= 8 && !candidateRoots.includes(parent)) {
                        candidateRoots.push(parent);
                    }
                    parent = parent.parentElement;
                }
                const name = el.getAttribute('name');
                let groupRoot = candidateRoots.find(root => {
                    const controls = controlsIn(root);
                    const sameNamed = name ? controls.filter(input => input.getAttribute('name') === name) : [];
                    return sameNamed.length >= 2 || (controls.includes(el) && controls.length >= 2 && controls.length <= 8);
                });
                if (!groupRoot && name) {
                    const sameName = Array.from(document.querySelectorAll(`input[type="${cssEscape(fieldType)}"][name="${cssEscape(name)}"]`));
                    if (sameName.length) {
                        groupRoot = sameName.reduce((root, input) => {
                            if (!root) return input.parentElement || input;
                            let cursor = root;
                            while (cursor && !cursor.contains(input)) cursor = cursor.parentElement;
                            return cursor || root;
                        }, null);
                    }
                }
                groupRoot = groupRoot || el.closest('fieldset, .fb-dash-form-element, [data-test-form-element]') || el.parentElement || el;
                const rootControls = controlsIn(groupRoot);
                const sameNamedInRoot = name ? rootControls.filter(input => input.getAttribute('name') === name) : [];
                let controls = sameNamedInRoot.length >= 2 ? sameNamedInRoot : rootControls;
                controls = Array.from(new Set([...controls, el])).filter(input => {
                    const type = (input.type || '').toLowerCase();
                    return type === 'radio' || type === 'checkbox';
                });
                const validationNodes = () => Array.from(groupRoot?.querySelectorAll(
                    '.artdeco-inline-feedback--error, .artdeco-inline-feedback__message, .fb-dash-form-element__error, [role="alert"], .error, .invalid-feedback'
                ) || []);
                const validationMessages = () => validationNodes()
                    .map(node => String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean);
                const hasSelectionValidation = () => validationMessages().some(message =>
                    /please make a selection|required|select an option|must select|choose/i.test(message)
                );
                const stateFor = input => {
                    const labelElement = labelElementFor(input);
                    const optionContainer = optionContainerFor(input);
                    const rect = input.getBoundingClientRect();
                    return {
                        id: input.id || '',
                        name: input.getAttribute('name') || '',
                        type: input.type || '',
                        value: input.value || '',
                        label: labelFor(input).replace(/\\s+/g, ' ').trim(),
                        checked: Boolean(input.checked),
                        aria_checked: input.getAttribute('aria-checked')
                            || labelElement?.getAttribute('aria-checked')
                            || optionContainer?.getAttribute('aria-checked')
                            || '',
                        disabled: Boolean(input.disabled || input.getAttribute('aria-disabled') === 'true'),
                        visible: elementVisible(input) || elementVisible(labelElement) || elementVisible(optionContainer),
                        class_name: `${input.className || ''} ${labelElement?.className || ''} ${optionContainer?.className || ''}`.trim(),
                        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                        html: (optionContainer?.outerHTML || input.outerHTML || '').replace(/\\s+/g, ' ').slice(0, 1200),
                    };
                };
                const dumpControls = () => controls.map(input => stateFor(input));
                const optionLabels = controls.map(input => labelFor(input).replace(/\\s+/g, ' ').trim()).filter(Boolean);
                const normalizedLabels = controls.map(input => ({
                    input,
                    label: labelFor(input),
                    normalized: normalize(labelFor(input)),
                    state: stateFor(input),
                }));
                const answerMatches = item => {
                    const label = item.normalized;
                    const value = normalize(item.input.value);
                    const yesNo = targetText === 'yes' || targetText === 'no';
                    if (label === targetText || value === targetText) return true;
                    if (yesNo && label.split(/\\s+/)[0] === targetText) return true;
                    if (!yesNo && (label.includes(targetText) || targetText.includes(label))) return true;
                    return false;
                };
                const candidates = normalizedLabels.filter(answerMatches).sort((left, right) => {
                    const leftScore = (left.state.visible ? 2 : 0) + (left.state.disabled ? -4 : 0) + (left.state.label ? 1 : 0);
                    const rightScore = (right.state.visible ? 2 : 0) + (right.state.disabled ? -4 : 0) + (right.state.label ? 1 : 0);
                    return rightScore - leftScore;
                });
                const target = candidates[0]?.input;
                const beforeControls = dumpControls();
                const beforeValidation = validationMessages().join(' | ');
                const groupHtml = (groupRoot?.outerHTML || '').replace(/\\s+/g, ' ').slice(0, 5000);
                const providerName = (() => {
                    const href = String(window.location?.href || '').toLowerCase();
                    const html = String(groupRoot?.outerHTML || '').toLowerCase();
                    if (href.includes('greenhouse') || html.includes('greenhouse')) return 'greenhouse';
                    if (href.includes('workable') || html.includes('workable')) return 'workable';
                    if (href.includes('bullhorn') || html.includes('bullhorn')) return 'bullhorn';
                    if (href.includes('linkedin.com') || html.includes('jobs-easy-apply')) return 'linkedin_native';
                    return 'unknown';
                })();
                const activeElementSummary = () => {
                    const active = document.activeElement;
                    if (!active) return {};
                    return {
                        tag: active.tagName,
                        id: active.id || '',
                        name: active.getAttribute('name') || '',
                        value: active.getAttribute('value') || '',
                        text: String(active.innerText || active.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
                    };
                };
                if (!target) {
                    return {
                        success: false,
                        final_write_status: 'failed',
                        option_labels_found: optionLabels,
                        selected_option_label: null,
                        click_strategy_used: null,
                        checked_state_before: null,
                        aria_state_before: null,
                        checked_state_after: null,
                        aria_state_after: null,
                        validation_message_before: beforeValidation,
                        validation_message_after: beforeValidation,
                        attempts: [],
                        group_diagnostics: {
                            question_text: questionTextFor(groupRoot),
                            group_outer_html: groupHtml,
                            provider_name: providerName,
                            inputs_before: beforeControls,
                            inputs_after: beforeControls,
                            active_element_after: activeElementSummary(),
                            nested_iframe_count: groupRoot?.querySelectorAll?.('iframe')?.length || 0,
                            shadow_host_count: Array.from(groupRoot?.querySelectorAll?.('*') || []).filter(node => node.shadowRoot).length,
                        },
                        reason: `No radio option matched answer "${payload.answer}".`,
                    };
                }
                const beforeState = stateFor(target);
                const attempts = [];
                const setNativeChecked = input => {
                    const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'checked');
                    if (descriptor?.set) {
                        descriptor.set.call(input, true);
                    } else {
                        input.checked = true;
                    }
                };
                const dispatchActivationEvents = node => {
                    if (!node || !(node instanceof Element)) return false;
                    const rect = node.getBoundingClientRect();
                    const x = rect.left + Math.max(1, Math.min(rect.width / 2, rect.width - 1));
                    const y = rect.top + Math.max(1, Math.min(rect.height / 2, rect.height - 1));
                    for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                        const eventInit = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y };
                        if (type.startsWith('pointer') && window.PointerEvent) {
                            node.dispatchEvent(new PointerEvent(type, { ...eventInit, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
                        } else {
                            node.dispatchEvent(new MouseEvent(type, eventInit));
                        }
                    }
                    return true;
                };
                const dispatchValueEvents = node => {
                    if (!node || !(node instanceof Element)) return false;
                    node.dispatchEvent(new Event('input', { bubbles: true, cancelable: true, composed: true }));
                    node.dispatchEvent(new Event('change', { bubbles: true, cancelable: true, composed: true }));
                    node.dispatchEvent(new FocusEvent('blur', { bubbles: true, composed: true }));
                    return true;
                };
                const blurAwayFromGroup = () => {
                    const focusTarget = groupRoot?.querySelector?.('button, input:not([type="radio"]):not([type="checkbox"]), textarea, select, a[href]')
                        || groupRoot?.parentElement?.querySelector?.('button, input:not([type="radio"]):not([type="checkbox"]), textarea, select, a[href]')
                        || document.body;
                    try {
                        focusTarget?.focus?.();
                    } catch (_) {
                        document.body?.focus?.();
                    }
                };
                const emitChoiceEvents = input => {
                    const label = labelElementFor(input);
                    const container = optionContainerFor(input);
                    dispatchActivationEvents(input);
                    dispatchActivationEvents(label);
                    dispatchActivationEvents(container);
                    dispatchValueEvents(input);
                    dispatchValueEvents(label);
                    dispatchValueEvents(container);
                    dispatchValueEvents(groupRoot);
                    blurAwayFromGroup();
                };
                const selectedControl = () => controls.find(input => input.checked || normalize(input.getAttribute('aria-checked')) === 'true');
                const verify = strategy => {
                    const afterState = stateFor(target);
                    const selected = selectedControl();
                    const selectedLabel = selected ? normalize(labelFor(selected)) : '';
                    const selectedValue = selected ? normalize(selected.value) : '';
                    const selectedMatchesTarget = selected === target || selectedLabel === targetText || selectedValue === targetText;
                    const afterValidation = validationMessages().join(' | ');
                    const stateAccepted = Boolean((afterState.checked || normalize(afterState.aria_checked) === 'true') && selectedMatchesTarget);
                    const accepted = Boolean(stateAccepted && !hasSelectionValidation());
                    attempts.push({
                        strategy,
                        checked: afterState.checked,
                        aria_checked: afterState.aria_checked,
                        selected_label: selected ? labelFor(selected).replace(/\\s+/g, ' ').trim() : '',
                        validation_message: afterValidation,
                        accepted,
                    });
                    return { accepted, afterState, afterValidation };
                };
                const labelTextClick = () => {
                    const targetLabel = normalize(labelFor(target));
                    const labels = Array.from(groupRoot?.querySelectorAll?.('label, span, div, p') || [])
                        .filter(node => normalize(node.innerText || node.textContent) === targetLabel || normalize(node.innerText || node.textContent).split(/\\s+/)[0] === targetText)
                        .sort((left, right) => {
                            const leftRect = left.getBoundingClientRect();
                            const rightRect = right.getBoundingClientRect();
                            return (rightRect.width * rightRect.height) - (leftRect.width * leftRect.height);
                        });
                    const label = labels[0] || labelElementFor(target);
                    if (!label) return false;
                    label.scrollIntoView?.({ block: 'center', inline: 'nearest' });
                    label.click();
                    dispatchActivationEvents(label);
                    dispatchValueEvents(label);
                    dispatchValueEvents(target);
                    blurAwayFromGroup();
                    return true;
                };
                const strategies = [
                    {
                        name: 'label_click',
                        run: () => {
                            const label = labelElementFor(target) || target.closest('label');
                            if (!label) return false;
                            label.scrollIntoView?.({ block: 'center', inline: 'nearest' });
                            label.click();
                            return true;
                        },
                    },
                    {
                        name: 'parent_container_click',
                        run: () => {
                            const container = optionContainerFor(target);
                            if (!container || container === groupRoot) return false;
                            container.scrollIntoView?.({ block: 'center', inline: 'nearest' });
                            container.click();
                            return true;
                        },
                    },
                    {
                        name: 'input_click',
                        run: () => {
                            target.scrollIntoView?.({ block: 'center', inline: 'nearest' });
                            target.focus();
                            target.click();
                            return true;
                        },
                    },
                    {
                        name: 'keyboard_space',
                        run: () => {
                            target.focus();
                            target.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true }));
                            target.dispatchEvent(new KeyboardEvent('keyup', { key: ' ', code: 'Space', bubbles: true, cancelable: true }));
                            return true;
                        },
                    },
                    {
                        name: 'native_checked_change',
                        run: () => {
                            if (fieldType === 'radio') {
                                controls.forEach(input => {
                                    if (input !== target && input.name === target.name) {
                                        input.checked = false;
                                        input.setAttribute('aria-checked', 'false');
                                    }
                                });
                            }
                            setNativeChecked(target);
                            target.setAttribute('aria-checked', 'true');
                            emitChoiceEvents(target);
                            return true;
                        },
                    },
                    {
                        name: 'post_selection_validation_recovery',
                        run: () => {
                            setNativeChecked(target);
                            target.setAttribute('aria-checked', 'true');
                            emitChoiceEvents(target);
                            dispatchValueEvents(groupRoot);
                            blurAwayFromGroup();
                            return true;
                        },
                    },
                    {
                        name: 'visible_label_text_recovery',
                        run: () => labelTextClick(),
                    },
                ];
                let finalState = beforeState;
                let finalValidation = beforeValidation;
                let selectedStrategy = null;
                for (const strategy of strategies) {
                    let attempted = false;
                    try {
                        attempted = Boolean(strategy.run());
                        if (attempted && strategy.name !== 'native_checked_change') {
                            target.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                            target.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                            target.dispatchEvent(new Event('blur', { bubbles: true, composed: true }));
                        }
                    } catch (error) {
                        attempts.push({
                            strategy: strategy.name,
                            accepted: false,
                            error: String(error?.message || error),
                        });
                        continue;
                    }
                    if (!attempted) {
                        attempts.push({
                            strategy: strategy.name,
                            accepted: false,
                            skipped: true,
                        });
                        continue;
                    }
                    await sleep(220);
                    const verified = verify(strategy.name);
                    finalState = verified.afterState;
                    finalValidation = verified.afterValidation;
                    if (verified.accepted) {
                        selectedStrategy = strategy.name;
                        break;
                    }
                }
                const afterControls = dumpControls();
                const success = Boolean(selectedStrategy);
                return {
                    success,
                    final_write_status: success ? 'success' : 'failed',
                    option_labels_found: optionLabels,
                    selected_option_label: labelFor(target).replace(/\\s+/g, ' ').trim(),
                    click_strategy_used: selectedStrategy,
                    checked_state_before: beforeState.checked,
                    aria_state_before: beforeState.aria_checked,
                    checked_state_after: finalState.checked,
                    aria_state_after: finalState.aria_checked,
                    validation_message_before: beforeValidation,
                    validation_message_after: finalValidation,
                    attempts,
                    group_diagnostics: {
                        question_text: questionTextFor(groupRoot),
                        group_outer_html: groupHtml,
                        provider_name: providerName,
                        group_control_count: controls.length,
                        group_root_tag: groupRoot?.tagName || '',
                        group_root_class: groupRoot?.className || '',
                        group_root_id: groupRoot?.id || '',
                        inputs_before: beforeControls,
                        inputs_after: afterControls,
                        active_element_after: activeElementSummary(),
                        selected_input_outer_html: (target.outerHTML || '').replace(/\\s+/g, ' ').slice(0, 1600),
                        exact_label_clicked: (labelElementFor(target)?.outerHTML || optionContainerFor(target)?.outerHTML || '').replace(/\\s+/g, ' ').slice(0, 1600),
                        nested_iframe_count: groupRoot?.querySelectorAll?.('iframe')?.length || 0,
                        shadow_host_count: Array.from(groupRoot?.querySelectorAll?.('*') || []).filter(node => node.shadowRoot).length,
                    },
                    reason: success ? null : 'Radio option was clicked but LinkedIn did not accept the selection.',
                };
            }
        """

    def _click_choice_control_native(
        self,
        field,
        answer: str,
        options: list[str],
        page=None,
        job: JobOpportunity | None = None,
        question: str | None = None,
        normalized_field: str | None = None,
    ) -> dict:
        """Select a radio/checkbox option using trusted Playwright interactions.

        This is the primary path. Real (isTrusted) browser events are what React's
        controlled inputs commit, which is the root cause of the previous
        "Please make a selection" failures where JS-synthesised events selected the
        control visually but never updated React state. We locate the option that
        matches ``answer``, click its visible label/container (or the input) with a
        real click, then verify the group actually committed the selection and the
        validation error cleared before reporting success.
        """
        result: dict = {
            "success": False,
            "interaction_path": "playwright_native",
            "final_write_status": "failed",
            "target_answer": answer,
            "option_labels_found": options,
            "question_label": question,
            "normalized_field": normalized_field,
            "job_id": getattr(job, "id", None),
            "company": getattr(job, "company", None),
            "attempts": [],
        }
        try:
            input_handle = field.evaluate_handle(self._choice_match_input_script(), {"answer": answer})
        except Exception as exc:
            result["reason"] = f"Native option match failed: {exc}"
            return result
        element = input_handle.as_element() if input_handle else None
        if element is None:
            result["reason"] = f'No radio/checkbox option matched answer "{answer}" for trusted interaction.'
            return result

        try:
            target_handle = element.evaluate_handle(self._choice_click_target_script())
            click_target = target_handle.as_element() or element
        except Exception:
            click_target = element

        def _verify() -> dict:
            try:
                return field.evaluate(self._choice_verify_script(), {"answer": answer}) or {}
            except Exception as exc:  # pragma: no cover - defensive
                return {"accepted": False, "error": str(exc)}

        strategies = [
            ("label_click", lambda: self._native_click_element(click_target)),
            ("input_check", lambda: self._native_check_element(element)),
            ("input_click_force", lambda: self._native_click_element(element, force=True)),
        ]
        selected_strategy = None
        verification: dict = {}
        for name, run in strategies:
            try:
                ran = run()
            except Exception as exc:
                result["attempts"].append({"strategy": name, "ran": False, "error": str(exc)})
                continue
            if page is not None:
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    pass
            verification = _verify()
            result["attempts"].append(
                {
                    "strategy": name,
                    "ran": bool(ran),
                    "checked": verification.get("checked"),
                    "selected_label": verification.get("selected_label"),
                    "validation_messages": verification.get("validation_messages"),
                    "accepted": verification.get("accepted"),
                }
            )
            if verification.get("accepted"):
                selected_strategy = name
                break

        result["success"] = bool(selected_strategy)
        result["final_write_status"] = "success" if selected_strategy else "failed"
        result["click_strategy_used"] = selected_strategy
        result["selected_option_label"] = verification.get("selected_label")
        result["checked_state_after"] = verification.get("checked")
        result["validation_message_after"] = " | ".join(verification.get("validation_messages") or [])
        # React state is only considered committed when the group reports the option
        # checked AND the required-selection validation cleared.
        result["react_state_committed"] = bool(selected_strategy) and bool(verification.get("checked"))
        if not selected_strategy:
            result["reason"] = (
                verification.get("error")
                or "Trusted interaction clicked the option but LinkedIn did not commit the selection."
            )
        return result

    def _native_click_element(self, element, force: bool = False) -> bool:
        if element is None:
            return False
        try:
            element.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        element.click(force=force, timeout=4000)
        return True

    def _native_check_element(self, element) -> bool:
        if element is None:
            return False
        try:
            element.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        element.check(force=True, timeout=4000)
        return True

    def _choice_match_input_script(self) -> str:
        return """
            (el, payload) => {
                const normalize = v => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const cssEsc = v => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v || '');
                const target = normalize(payload.answer);
                const name = el.getAttribute('name');
                const roots = ['fieldset', '.fb-dash-form-element', '[data-test-form-element]', '[data-test-form-builder-question]', '.jobs-easy-apply-form-section__grouping', '.jobs-easy-apply-form-section', '[role="group"]']
                    .map(sel => el.closest(sel)).filter(Boolean);
                let group = roots.find(root => root.querySelectorAll('input[type="radio"], input[type="checkbox"]').length >= 1);
                if (!group) {
                    let parent = el.parentElement;
                    for (let i = 0; parent && i < 8; i += 1) {
                        const count = parent.querySelectorAll('input[type="radio"], input[type="checkbox"]').length;
                        if (count >= 1 && count <= 8) { group = parent; break; }
                        parent = parent.parentElement;
                    }
                }
                group = group || el.parentElement || el;
                let controls = Array.from(group.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
                if (name) {
                    const sameName = controls.filter(input => input.getAttribute('name') === name);
                    if (sameName.length) controls = sameName;
                }
                if (!controls.includes(el)) controls.push(el);
                const labelText = input => {
                    const id = input.id;
                    const byFor = id ? document.querySelector(`label[for="${cssEsc(id)}"]`) : null;
                    const labelledBy = input.getAttribute('aria-labelledby');
                    const aria = labelledBy ? labelledBy.split(/\\s+/).map(part => document.getElementById(part)?.innerText || '').join(' ') : '';
                    const closestLabel = input.closest('label');
                    const container = input.closest('label, .fb-radio, .artdeco-radio, .jobs-easy-apply-form-section__option, li, div');
                    return String(byFor?.innerText || closestLabel?.innerText || aria || input.getAttribute('aria-label') || container?.innerText || input.value || '');
                };
                const yesNo = target === 'yes' || target === 'no';
                const matches = input => {
                    const label = normalize(labelText(input));
                    const value = normalize(input.value);
                    if (label === target || value === target) return true;
                    if (yesNo && label.split(/\\s+/)[0] === target) return true;
                    if (!yesNo && label && (label.includes(target) || target.includes(label))) return true;
                    return false;
                };
                const found = controls.filter(matches).sort((a, b) => {
                    const va = a.offsetParent !== null ? 1 : 0;
                    const vb = b.offsetParent !== null ? 1 : 0;
                    return vb - va;
                });
                return found[0] || null;
            }
        """

    def _choice_click_target_script(self) -> str:
        return """
            (input) => {
                const cssEsc = v => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v || '');
                const isVisible = node => {
                    if (!node || !(node instanceof Element)) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const id = input.id;
                const byFor = id ? document.querySelector(`label[for="${cssEsc(id)}"]`) : null;
                const closestLabel = input.closest('label');
                const container = input.closest('.fb-radio, .artdeco-radio, .jobs-easy-apply-form-section__option');
                const candidates = [byFor, closestLabel, container].filter(Boolean);
                const visibleCandidate = candidates.find(isVisible);
                return visibleCandidate || (isVisible(input) ? input : null) || candidates[0] || input;
            }
        """

    def _choice_verify_script(self) -> str:
        return """
            (el, payload) => {
                const normalize = v => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const cssEsc = v => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v || '');
                const target = normalize(payload.answer);
                const name = el.getAttribute('name');
                const roots = ['fieldset', '.fb-dash-form-element', '[data-test-form-element]', '[data-test-form-builder-question]', '.jobs-easy-apply-form-section__grouping', '.jobs-easy-apply-form-section', '[role="group"]']
                    .map(sel => el.closest(sel)).filter(Boolean);
                let group = roots.find(root => root.querySelectorAll('input[type="radio"], input[type="checkbox"]').length >= 1);
                if (!group) {
                    let parent = el.parentElement;
                    for (let i = 0; parent && i < 8; i += 1) {
                        const count = parent.querySelectorAll('input[type="radio"], input[type="checkbox"]').length;
                        if (count >= 1 && count <= 8) { group = parent; break; }
                        parent = parent.parentElement;
                    }
                }
                group = group || el.parentElement || el;
                let controls = Array.from(group.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
                if (name) {
                    const sameName = controls.filter(input => input.getAttribute('name') === name);
                    if (sameName.length) controls = sameName;
                }
                if (!controls.includes(el)) controls.push(el);
                const labelText = input => {
                    const id = input.id;
                    const byFor = id ? document.querySelector(`label[for="${cssEsc(id)}"]`) : null;
                    const closestLabel = input.closest('label');
                    const container = input.closest('label, .fb-radio, .artdeco-radio, .jobs-easy-apply-form-section__option, li, div');
                    return String(byFor?.innerText || closestLabel?.innerText || input.getAttribute('aria-label') || container?.innerText || input.value || '');
                };
                const selected = controls.find(input => input.checked || normalize(input.getAttribute('aria-checked')) === 'true');
                const selectedLabel = selected ? normalize(labelText(selected)) : '';
                const selectedValue = selected ? normalize(selected.value) : '';
                const yesNo = target === 'yes' || target === 'no';
                const matchesTarget = Boolean(selected) && (
                    selectedLabel === target
                    || selectedValue === target
                    || (yesNo && selectedLabel.split(/\\s+/)[0] === target)
                    || (!yesNo && selectedLabel && (selectedLabel.includes(target) || target.includes(selectedLabel)))
                );
                const validationNodes = Array.from(group.querySelectorAll('.artdeco-inline-feedback--error, .artdeco-inline-feedback__message, .fb-dash-form-element__error, [role="alert"], .error, .invalid-feedback') || []);
                const validationMessages = validationNodes
                    .map(node => String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean);
                const selectionError = validationMessages.some(message => /please make a selection|required|select an option|must select|choose/i.test(message));
                return {
                    checked: Boolean(selected),
                    selected_label: selected ? labelText(selected).replace(/\\s+/g, ' ').trim() : '',
                    matches_target: Boolean(matchesTarget),
                    validation_messages: validationMessages,
                    selection_error: selectionError,
                    accepted: Boolean(matchesTarget && !selectionError),
                };
            }
        """

    def _click_choice_control_with_writer(
        self,
        field,
        answer: str,
        options: list[str],
        page=None,
        job: JobOpportunity | None = None,
        question: str | None = None,
        normalized_field: str | None = None,
        radio_debug_dir: Path | None = None,
    ) -> dict:
        payload = {
            "answer": answer,
            "options": options,
            "question": question,
            "normalized_field": normalized_field,
        }
        result: dict
        try:
            raw_result = field.evaluate(self._choice_control_writer_script(), payload)
            if isinstance(raw_result, bool):
                result = {
                    "success": raw_result,
                    "final_write_status": "success" if raw_result else "failed",
                }
            else:
                result = dict(raw_result or {})
        except Exception as exc:
            result = {
                "success": False,
                "final_write_status": "failed",
                "reason": f"Radio writer execution failed: {exc}",
                "option_labels_found": options,
            }
        result.update(
            {
                "job_id": getattr(job, "id", None),
                "company": getattr(job, "company", None),
                "question_label": question,
                "normalized_field": normalized_field,
                "target_answer": answer,
            }
        )
        if not result.get("success"):
            result["screenshot_path"] = self._capture_radio_writer_failure(page, radio_debug_dir, job)
        self._save_radio_writer_debug(radio_debug_dir, result)
        return result

    def _click_choice_control(
        self,
        field,
        answer: str,
        options: list[str],
        page=None,
        job: JobOpportunity | None = None,
        question: str | None = None,
        normalized_field: str | None = None,
        radio_debug_dir: Path | None = None,
    ) -> dict:
        # Primary path: trusted Playwright interaction (real browser events that
        # React commits). The JS writer is kept only as a fallback for cases the
        # trusted path cannot select/verify (e.g. exotic custom widgets).
        try:
            native = self._click_choice_control_native(
                field,
                answer,
                options,
                page=page,
                job=job,
                question=question,
                normalized_field=normalized_field,
            )
        except Exception as exc:  # pragma: no cover - defensive
            native = {
                "success": False,
                "interaction_path": "playwright_native",
                "final_write_status": "failed",
                "reason": f"Native interaction raised: {exc}",
                "attempts": [],
            }
        if native.get("success"):
            self._save_radio_writer_debug(radio_debug_dir, native)
            return native

        writer = self._click_choice_control_with_writer(
            field,
            answer,
            options,
            page=page,
            job=job,
            question=question,
            normalized_field=normalized_field,
            radio_debug_dir=radio_debug_dir,
        )
        writer["interaction_path"] = "js_writer_fallback"
        writer["native_attempt"] = native
        return writer
        payload = {
            "answer": answer,
            "options": options,
        }
        result: dict
        try:
            raw_result = field.evaluate(
                """async (el, payload) => {
                        /* radioWriterPayload */
                        const normalize = value => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .replace(/[’']/g, "'")
                            .trim()
                            .toLowerCase();
                        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
                        const targetText = normalize(payload.answer);
                        const fieldType = (el.type || '').toLowerCase();
                        const groupContainer = el.closest('fieldset, .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, [data-test-form-element], .artdeco-card, div') || el.parentElement || el;
                        const name = el.getAttribute('name');
                        const sameName = name && window.CSS && CSS.escape
                            ? Array.from(document.querySelectorAll(`input[type="${CSS.escape(fieldType)}"][name="${CSS.escape(name)}"]`))
                            : [];
                        const controls = Array.from(new Set([
                            ...(sameName.length ? sameName : []),
                            ...Array.from(groupContainer?.querySelectorAll('input[type="radio"], input[type="checkbox"]') || []),
                            el,
                        ])).filter(input => {
                            const type = (input.type || '').toLowerCase();
                            return type === 'radio' || type === 'checkbox';
                        });
                        const labelFor = input => {
                            const id = input.id;
                            const byFor = id && window.CSS && CSS.escape ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                            const labelledBy = input.getAttribute('aria-labelledby');
                            const ariaLabel = labelledBy
                                ? labelledBy.split(/\\s+/).map(idPart => document.getElementById(idPart)?.innerText || '').join(' ')
                                : '';
                            const closestLabel = input.closest('label');
                            const optionContainer = input.closest('label, .fb-radio, .artdeco-radio, .jobs-easy-apply-form-section__option, li, div');
                            return byFor?.innerText || closestLabel?.innerText || ariaLabel || optionContainer?.innerText || input.value || '';
                        };
                        const labelElementFor = input => {
                            const id = input.id;
                            return id && window.CSS && CSS.escape ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                        };
                        const optionContainerFor = input => (
                            input.closest('label, .fb-radio, .artdeco-radio, .jobs-easy-apply-form-section__option, li, div') || input.parentElement || input
                        );
                        const validationMessages = () => Array.from(groupContainer?.querySelectorAll(
                            '.artdeco-inline-feedback--error, .artdeco-inline-feedback__message, .fb-dash-form-element__error, [role="alert"]'
                        ) || [])
                            .map(node => String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim())
                            .filter(Boolean);
                        const hasSelectionValidation = () => validationMessages().some(message =>
                            /please make a selection|required|select an option/i.test(message)
                        );
                        const stateFor = input => {
                            const labelElement = labelElementFor(input);
                            const optionContainer = optionContainerFor(input);
                            return {
                                checked: Boolean(input.checked),
                                aria_checked: input.getAttribute('aria-checked')
                                    || labelElement?.getAttribute('aria-checked')
                                    || optionContainer?.getAttribute('aria-checked')
                                    || '',
                                value: input.value || '',
                                class_name: `${input.className || ''} ${labelElement?.className || ''} ${optionContainer?.className || ''}`.trim(),
                            };
                        };
                        const optionLabels = controls.map(input => labelFor(input).replace(/\\s+/g, ' ').trim()).filter(Boolean);
                        const normalizedLabels = controls.map(input => ({ input, label: labelFor(input), normalized: normalize(labelFor(input)) }));
                        const exact = normalizedLabels.find(item => item.normalized === targetText)?.input;
                        const yesNo = targetText === 'yes' || targetText === 'no';
                        const target = exact
                            || normalizedLabels.find(item => yesNo && item.normalized.split(/\\s+/)[0] === targetText)?.input
                            || normalizedLabels.find(item => !yesNo && (item.normalized.includes(targetText) || targetText.includes(item.normalized)))?.input;
                        if (!target) {
                            return {
                                success: false,
                                final_write_status: 'failed',
                                option_labels_found: optionLabels,
                                selected_option_label: null,
                                click_strategy_used: null,
                                checked_state_before: null,
                                aria_state_before: null,
                                checked_state_after: null,
                                aria_state_after: null,
                                validation_message_before: validationMessages().join(' | '),
                                validation_message_after: validationMessages().join(' | '),
                                attempts: [],
                                reason: `No radio option matched answer "${payload.answer}".`,
                            };
                        }
                        const beforeState = stateFor(target);
                        const beforeValidation = validationMessages().join(' | ');
                        const attempts = [];
                        const verify = strategy => {
                            const afterState = stateFor(target);
                            const afterValidation = validationMessages().join(' | ');
                            const stateAccepted = afterState.checked || normalize(afterState.aria_checked) === 'true';
                            const accepted = Boolean(stateAccepted && !hasSelectionValidation());
                            attempts.push({
                                strategy,
                                checked: afterState.checked,
                                aria_checked: afterState.aria_checked,
                                validation_message: afterValidation,
                                accepted,
                            });
                            return { accepted, afterState, afterValidation };
                        };
                        const emitChoiceEvents = input => {
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            input.dispatchEvent(new Event('blur', { bubbles: true }));
                        };
                        const strategies = [
                            {
                                name: 'label_click',
                                run: () => {
                                    const label = labelElementFor(target) || target.closest('label');
                                    if (!label) return false;
                                    label.click();
                                    return true;
                                },
                            },
                            {
                                name: 'parent_container_click',
                                run: () => {
                                    const container = optionContainerFor(target);
                                    if (!container || container === groupContainer) return false;
                                    container.click();
                                    return true;
                                },
                            },
                            {
                                name: 'input_click',
                                run: () => {
                                    target.focus();
                                    target.click();
                                    return true;
                                },
                            },
                            {
                                name: 'keyboard_space',
                                run: () => {
                                    target.focus();
                                    target.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true }));
                                    target.dispatchEvent(new KeyboardEvent('keyup', { key: ' ', code: 'Space', bubbles: true }));
                                    return true;
                                },
                            },
                            {
                                name: 'hidden_input_change',
                                run: () => {
                                    if (fieldType === 'radio') {
                                        controls.forEach(input => {
                                            if (input !== target && input.name === target.name) {
                                                input.checked = false;
                                            }
                                        });
                                    }
                                    target.checked = true;
                                    target.setAttribute('aria-checked', 'true');
                                    emitChoiceEvents(target);
                                    return true;
                                },
                            },
                        ];
                        let finalState = beforeState;
                        let finalValidation = beforeValidation;
                        let selectedStrategy = null;
                        for (const strategy of strategies) {
                            let attempted = false;
                            try {
                                attempted = Boolean(strategy.run());
                                if (attempted) {
                                    emitChoiceEvents(target);
                                }
                            } catch (error) {
                                attempts.push({
                                    strategy: strategy.name,
                                    accepted: false,
                                    error: String(error?.message || error),
                                });
                                continue;
                            }
                            if (!attempted) {
                                attempts.push({
                                    strategy: strategy.name,
                                    accepted: false,
                                    skipped: true,
                                });
                                continue;
                            }
                            await sleep(180);
                            const verified = verify(strategy.name);
                            finalState = verified.afterState;
                            finalValidation = verified.afterValidation;
                            if (verified.accepted) {
                                selectedStrategy = strategy.name;
                                break;
                            }
                        }
                        const success = Boolean(selectedStrategy);
                        return {
                            success,
                            final_write_status: success ? 'success' : 'failed',
                            option_labels_found: optionLabels,
                            selected_option_label: labelFor(target).replace(/\\s+/g, ' ').trim(),
                            click_strategy_used: selectedStrategy,
                            checked_state_before: beforeState.checked,
                            aria_state_before: beforeState.aria_checked,
                            checked_state_after: finalState.checked,
                            aria_state_after: finalState.aria_checked,
                            validation_message_before: beforeValidation,
                            validation_message_after: finalValidation,
                            attempts,
                            reason: success ? null : 'Radio option was clicked but LinkedIn did not accept the selection.',
                        };
                    }""",
                payload,
            )
            if isinstance(raw_result, bool):
                result = {"success": raw_result, "final_write_status": "success" if raw_result else "failed"}
            else:
                result = dict(raw_result or {})
        except Exception as exc:
            result = {
                "success": False,
                "final_write_status": "failed",
                "reason": f"Radio writer execution failed: {exc}",
                "option_labels_found": options,
            }
        result.update(
            {
                "job_id": getattr(job, "id", None),
                "company": getattr(job, "company", None),
                "question_label": question,
                "normalized_field": normalized_field,
                "target_answer": answer,
            }
        )
        if not result.get("success"):
            result["screenshot_path"] = self._capture_radio_writer_failure(page, radio_debug_dir, job)
        self._save_radio_writer_debug(radio_debug_dir, result)
        return result

    def _closest_option(self, answer: str, options: list[str]) -> str:
        if not options:
            return answer
        answer_lower = answer.lower()
        for option in options:
            if option.lower() == answer_lower:
                return option
        for option in options:
            if answer_lower in option.lower() or option.lower() in answer_lower:
                return option
        return options[0]

    def _matching_saved_screening_answer(self, question: str, category: str, answers: list[dict]) -> dict | None:
        return self.screening.matching_saved_answer(question, category, answers)

    def _normalize_screening_question(self, question: str) -> str:
        return self.screening.normalize_question(question)

    def _extract_skill_from_experience_question(self, question: str) -> str | None:
        return self.screening.extract_skill_from_experience_question(question)

    def _estimate_skill_years(
        self,
        skill: str,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
    ) -> dict:
        return self.screening.estimate_skill_years(skill, resume, candidate_profile)

    def _extract_location_from_resume(self, resume: ResumeProfile) -> str | None:
        return self.screening.extract_location_from_resume(resume)

    def _save_screening_debug(
        self,
        debug_dir: Path | None,
        detected: list[dict],
        events: list[dict],
        field_traces: list[dict] | None = None,
    ) -> None:
        if not debug_dir:
            return
        debug_dir.mkdir(parents=True, exist_ok=True)
        selected = [event for event in events if event.get("selected_answer")]
        all_detected = detected or [
            {
                "question_text": event.get("question_text"),
                "original_label": event.get("original_label") or event.get("question_text"),
                "normalized_field": event.get("normalized_field"),
                "category": event.get("category"),
                "field_type": event.get("field_type"),
                "detected_control_type": event.get("detected_control_type") or event.get("field_type"),
                "options": event.get("options", []),
            }
            for event in events
            if event.get("question_text")
        ]
        defaults = [event for event in selected if event.get("aggressive_default_used")]
        generated = [event for event in selected if event.get("source") in {"AI Generated", "AI Estimated Salary"}]
        skill_questions = [
            {
                "original_question": event.get("question_text"),
                "normalized_skill": (event.get("evidence") or {}).get("normalized_skill"),
                "field_type": event.get("field_type"),
                "mode_used": event.get("mode"),
            }
            for event in events
            if event.get("category") == "skill_years"
        ]
        skill_estimates = [
            {
                "original_question": event.get("question_text"),
                "normalized_skill": (event.get("evidence") or {}).get("normalized_skill"),
                "selected_years": event.get("selected_answer"),
                "source": event.get("source"),
                "confidence": event.get("confidence"),
                "evidence_used": (event.get("evidence") or {}).get("evidence_used"),
                "mode_used": event.get("mode"),
                "aggressive_default_used": event.get("aggressive_default_used"),
                "reason": event.get("reason"),
            }
            for event in selected
            if event.get("category") == "skill_years"
        ]
        needs_review = [event for event in events if not event.get("selected_answer")]
        confidence = [
            {
                "question_text": event.get("question_text"),
                "answer": event.get("selected_answer"),
                "source": event.get("source"),
                "confidence": event.get("confidence"),
                "currency": event.get("currency"),
                "reason": event.get("reason"),
            }
            for event in events
        ]
        radio_events = [event.get("radio_writer") for event in events if event.get("radio_writer")]
        files = {
            "questions_detected.json": all_detected,
            "evidence_used.json": [event.get("evidence", {}) for event in selected],
            "selected_answers.json": selected,
            "aggressive_defaults_used.json": defaults,
            "generated_text_answers.json": generated,
            "skill_experience_questions.json": skill_questions,
            "skill_experience_estimates.json": skill_estimates,
            "needs_review.json": needs_review,
            "confidence_scores.json": confidence,
            "field_decision_trace.json": field_traces or [self._screening_field_trace(event) for event in events],
        }
        if radio_events:
            files["radio_writer_events.json"] = radio_events
        for filename, payload in files.items():
            (debug_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _save_reasoning_debug(self, job: JobOpportunity, resume: ResumeProfile, events: list[dict]) -> None:
        reasoning_events = [event for event in events if event.get("reasoning_used")]
        if not reasoning_events:
            return
        debug_dir = self.reasoning.create_debug_dir(job)
        metadata = job.job_description_metadata or self.reasoning.analyze_job_description(job)
        comparison = self.reasoning.compare_resume_to_job(resume, job)
        files = {
            "job_description_analysis.json": metadata,
            "semantic_matches.json": comparison,
            "reasoning_steps.json": reasoning_events,
            "confidence_scores.json": [
                {
                    "question": event.get("question_text"),
                    "answer": event.get("selected_answer"),
                    "confidence": event.get("confidence"),
                    "reasoning_type": event.get("reasoning_type"),
                }
                for event in reasoning_events
            ],
            "evidence_used.json": [
                (event.get("evidence") or {}).get("evidence_used", [])
                for event in reasoning_events
            ],
            "question_reasoning.json": reasoning_events,
        }
        for filename, payload in files.items():
            (debug_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _field_label(self, field, index: int) -> str:
        try:
            return field.evaluate(
                """(el, fallback) => {
                    const id = el.id;
                    const byFor = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                    const wrapping = el.closest('label');
                    const fieldElement = el.closest('.fb-dash-form-element, .artdeco-text-input');
                    const fieldLabel = fieldElement?.querySelector(':scope > label, :scope > .artdeco-text-input--label, :scope > .fb-dash-form-element__label');
                    const fieldset = el.closest('fieldset');
                    const legend = fieldset?.querySelector(':scope > legend');
                    const parentLabel = el.parentElement?.querySelector('label, .artdeco-text-input--label, .fb-dash-form-element__label');
                    let previous = el.previousElementSibling;
                    let previousLabel = '';
                    for (let i = 0; previous && i < 4; i += 1) {
                        if (previous.matches?.('label, .artdeco-text-input--label, .fb-dash-form-element__label')) {
                            previousLabel = previous.innerText || '';
                            break;
                        }
                        previous = previous.previousElementSibling;
                    }
                    return (
                        byFor?.innerText ||
                        wrapping?.innerText ||
                        fieldLabel?.innerText ||
                        parentLabel?.innerText ||
                        previousLabel ||
                        legend?.innerText ||
                        el.getAttribute('aria-label') ||
                        el.getAttribute('name') ||
                        el.getAttribute('placeholder') ||
                        fallback
                    ).trim();
                }""",
                f"required field {index + 1}",
            )
        except Exception:
            return f"required field {index + 1}"

    def _save_form_diagnostics(
        self,
        page: Page,
        modal,
        evidence_dir: Path | None,
        debug_dir: Path,
        step: str,
    ) -> None:
        try:
            buttons = (
                modal.locator("button:visible").all_inner_texts()
                if modal is not None
                else []
            )
            inputs = (
                modal.locator("input:visible, textarea:visible, select:visible").evaluate_all(
                    """elements => elements.map((el, index) => ({
                        index,
                        tag: el.tagName,
                        type: el.type || null,
                        name: el.name || null,
                        aria_label: el.getAttribute('aria-label'),
                        placeholder: el.getAttribute('placeholder'),
                        required: el.required || false,
                        has_value: el.type === 'checkbox' || el.type === 'radio'
                            ? el.checked
                            : Boolean(el.value)
                    }))"""
                )
                if modal is not None
                else []
            )
            payload = {
                "step": step,
                "page_title": page.title(),
                "current_url": page.url,
                "modal_found": modal is not None,
                "visible_modal_buttons": [text.strip() for text in buttons if text.strip()],
                "visible_form_fields": inputs,
                "captured_at": datetime.utcnow().isoformat(),
            }
            (debug_dir / "form_diagnostic.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            if modal is not None:
                (debug_dir / "form_modal.html").write_text(
                    modal.evaluate("el => el.outerHTML"),
                    encoding="utf-8",
                )
            if evidence_dir:
                self._capture_screenshot(page, evidence_dir, f"failure_{step}")
        except Exception as exc:
            logger.warning("Could not save form diagnostics: %s", exc)

    def _confirmation_received(self, page: Page) -> bool:
        confirmation_text = re.compile(
            r"application (?:was )?sent|application submitted|your application|applied",
            re.I,
        )
        return page.get_by_text(confirmation_text).count() > 0

    def _create_evidence_dir(self, job: JobOpportunity) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", job.title).strip("_")[:60] or "job"
        evidence_dir = self.settings.live_test_output_dir / f"{timestamp}_job_{job.id}_{safe_title}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.live_test_output_dir / "screenshots" / evidence_dir.name).mkdir(
            parents=True,
            exist_ok=True,
        )
        return evidence_dir

    def _create_contact_debug_dir(self, job: JobOpportunity, linkedin_job_id: str) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_job_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(linkedin_job_id)).strip("_")
        safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", job.title).strip("_")[:40] or "job"
        contact_dir = self.settings.contact_debug_dir / timestamp / f"{safe_job_id or job.id}_{safe_title}"
        contact_dir.mkdir(parents=True, exist_ok=True)
        return contact_dir

    def _create_profile_apply_debug_dir(self, job: JobOpportunity, linkedin_job_id: str) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_job_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(linkedin_job_id)).strip("_")
        safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", job.title).strip("_")[:40] or "job"
        debug_dir = self.settings.profile_autofill_debug_dir / timestamp / f"{safe_job_id or job.id}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _create_form_navigation_debug_dir(self, job: JobOpportunity, linkedin_job_id: str) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_job_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(linkedin_job_id)).strip("_")
        safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", job.title).strip("_")[:40] or "job"
        debug_dir = self.settings.form_navigation_debug_dir / timestamp / f"{safe_job_id or job.id}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _create_screening_debug_dir(self, job: JobOpportunity, linkedin_job_id: str) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_job_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(linkedin_job_id)).strip("_")
        safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", job.title).strip("_")[:40] or "job"
        debug_dir = self.settings.screening_debug_dir / timestamp / f"{safe_job_id or job.id}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _create_radio_writer_debug_dir(self, job: JobOpportunity) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", job.title).strip("_")[:40] or "job"
        debug_dir = self.settings.radio_writer_debug_dir / timestamp / f"{job.id}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _capture_radio_writer_failure(self, page, debug_dir: Path | None, job: JobOpportunity | None) -> str | None:
        if not page or not debug_dir:
            return None
        try:
            safe_job_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(getattr(job, "id", "job"))).strip("_") or "job"
            screenshot_path = debug_dir / f"radio_write_failed_{safe_job_id}.png"
            try:
                page.screenshot(path=str(screenshot_path), full_page=True, timeout=5000)
            except TypeError:
                page.screenshot(path=str(screenshot_path), full_page=True)
            return str(screenshot_path)
        except Exception as exc:
            logger.warning("Could not capture radio writer failure screenshot: %s", exc)
            return None

    def _save_radio_writer_debug(self, debug_dir: Path | None, event: dict) -> None:
        if not debug_dir:
            return
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = debug_dir / "radio_writer_events.json"
        payload = {
            "captured_at": datetime.utcnow().isoformat(),
            "events": [],
        }
        if debug_file.exists():
            try:
                payload = json.loads(debug_file.read_text(encoding="utf-8"))
            except Exception:
                payload = {
                    "captured_at": datetime.utcnow().isoformat(),
                    "events": [],
                }
        payload.setdefault("events", []).append(event)
        payload["updated_at"] = datetime.utcnow().isoformat()
        debug_file.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _save_profile_apply_debug(self, debug_dir: Path | None, filename: str, payload: dict) -> None:
        if not debug_dir:
            return
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / filename).write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )

    def _candidate_profile_debug_payload(self, candidate_profile: CandidateProfile | None) -> dict:
        if candidate_profile is None:
            return {"profile_available": False}
        fields = [
            "first_name",
            "last_name",
            "email",
            "mobile_phone",
            "phone_country_code",
            "current_location",
            "linkedin_url",
            "portfolio_url",
            "github_url",
            "address",
            "current_job_title",
            "professional_headline",
            "current_company",
        ]
        return {
            "profile_available": True,
            "fields": {
                field: {
                    "value": getattr(candidate_profile, field, None),
                    **((candidate_profile.field_sources or {}).get(field, {})),
                }
                for field in fields
            },
            "professional_links": candidate_profile.professional_links or {},
            "previous_companies": candidate_profile.previous_companies or [],
            "certifications": candidate_profile.certifications or [],
        }

    def _capture_screenshot(self, page: Page, evidence_dir: Path | None, name: str) -> None:
        if not evidence_dir:
            return
        try:
            screenshot_dir = self.settings.live_test_output_dir / "screenshots" / evidence_dir.name
            try:
                page.screenshot(path=str(screenshot_dir / f"{name}.png"), full_page=True, timeout=5000)
            except TypeError:
                page.screenshot(path=str(screenshot_dir / f"{name}.png"), full_page=True)
        except Exception as exc:
            logger.warning("Could not capture %s screenshot: %s", name, exc)

    def _persist_run_log(
        self,
        evidence_dir: Path | None,
        job: JobOpportunity,
        dry_run: bool,
        current_step: str,
        run_log: list[dict],
        error: str | None = None,
        contact_events: list[dict] | None = None,
    ) -> None:
        if not evidence_dir:
            return
        contact_events = contact_events or []
        payload = {
            "job_id": job.id,
            "job_title": job.title,
            "company": job.company,
            "job_url": job.job_url,
            "application_date": datetime.utcnow().isoformat(),
            "dry_run": dry_run,
            "current_step": current_step,
            "error": error,
            "contact_fields_detected": self._contact_detected(contact_events),
            "contact_fields_filled": self._contact_filled(contact_events),
            "missing_contact_fields": self._contact_missing(contact_events),
            "events": run_log,
        }
        (evidence_dir / "application_log.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _log(self, run_log: list[dict], event: str, detail: str) -> None:
        run_log.append({"time": datetime.utcnow().isoformat(), "event": event, "detail": detail})
