from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from app.config import Settings
from app.models import ApplicationStatus, CandidateProfile, JobOpportunity, ResumeProfile
from app.services.find_matches import SearchVariation
from app.services.generalized_form_engine import GeneralizedFieldInput, GeneralizedFormEngine
from app.services.linkedin import AutomationResult
from app.services.screening_engine import ScreeningAnswerEngine, ScreeningQuestion


class DicePortalAdapter:
    portal_name = "dice"
    login_timeout_seconds = 600
    AUTH_COOKIE_RE = re.compile(r"(auth|access|refresh|id[_-]?token|jwt|session|sso)", re.I)
    ANONYMOUS_COOKIE_RE = re.compile(r"(anonymous|anon|guest|consent|preference|tracking|analytics|csrf|xsrf)", re.I)
    AUTH_STORAGE_RE = re.compile(r"(auth|access|refresh|id[_-]?token|jwt|session|sso)", re.I)
    CARD_SELECTORS = [
        "dhi-search-card",
        "[data-cy='search-card']",
        "[data-testid='job-search-card']",
        "[data-testid='job-card']",
        "[data-testid*='job-card']",
        "[class*='search-card']",
        "[class*='job-card']",
        "article",
        "li:has(a[href*='/job-detail/'])",
        "li:has(a[href*='/jobs/detail/'])",
        "div:has(> a[href*='/job-detail/'])",
    ]
    TITLE_SELECTORS = [
        "[data-cy='card-title-link']",
        "[data-cy='search-result-title']",
        "[data-testid*='title']",
        "a[href*='/job-detail/']",
        "a[href*='/jobs/detail/']",
        "h1",
        "h2",
        "h3",
    ]
    COMPANY_SELECTORS = [
        "[data-cy='search-result-company-name']",
        "[data-testid*='company']",
        "[class*='company']",
        "a[href*='/company/']",
    ]
    LOCATION_SELECTORS = [
        "[data-cy='search-result-location']",
        "[data-testid*='location']",
        "[class*='location']",
        "[aria-label*='location' i]",
    ]

    # Label patterns for candidate contact fields (portal-agnostic decisions come
    # from the shared engines; contact identity fields are filled from the profile).
    CONTACT_FIELD_PATTERNS = [
        ("first_name", r"first\s*name"),
        ("last_name", r"last\s*name|surname"),
        ("email", r"e[-\s]?mail"),
        ("mobile_phone", r"phone|mobile|contact\s*number"),
        ("current_location", r"location|city|where.*located"),
        ("linkedin_url", r"linkedin"),
    ]

    def __init__(self, settings: Settings):
        self.settings = settings
        self._screening_engine: ScreeningAnswerEngine | None = None
        self._form_engine: GeneralizedFormEngine | None = None

    @property
    def storage_state(self) -> Path:
        return self.settings.dice_storage_state

    @property
    def screening(self) -> ScreeningAnswerEngine:
        # Shared, portal-agnostic decision engine (same instance type LinkedIn uses).
        if self._screening_engine is None:
            self._screening_engine = ScreeningAnswerEngine(self.settings)
        return self._screening_engine

    @property
    def form_engine(self) -> GeneralizedFormEngine:
        if self._form_engine is None:
            self._form_engine = GeneralizedFormEngine(self.settings)
        return self._form_engine

    def _debug_dir(self) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        debug_dir = self.settings.dice_debug_dir / timestamp
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _write_debug(self, debug_dir: Path, filename: str, payload: dict | list) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def connect(self) -> dict:
        debug_dir = self._debug_dir()
        events: list[dict] = []

        def log_event(event: str, **extra) -> None:
            events.append({"event": event, "time": datetime.utcnow().isoformat(), **extra})
            self._write_debug(debug_dir, "connect.json", {"portal": self.portal_name, "events": events})

        log_event("login_started")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False, slow_mo=self.settings.linkedin_slow_mo_ms)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://www.dice.com/dashboard/login", wait_until="domcontentloaded", timeout=60000)
            deadline = time.monotonic() + self.login_timeout_seconds
            connected = False
            final_url = page.url
            try:
                while time.monotonic() < deadline:
                    if page.is_closed() or not browser.is_connected():
                        log_event("login_cancelled", reason="browser_closed")
                        break
                    final_url = page.url
                    if self._page_looks_logged_in(page) and self._context_has_authenticated_session(context):
                        context.storage_state(path=str(self.storage_state))
                        log_event("login_completed", url=final_url)
                        log_event("session_saved", storage_state=str(self.storage_state))
                        connected = True
                        break
                    page.wait_for_timeout(1000)
                else:
                    log_event("session_verification_failed", reason="login_timeout", url=final_url)
            except PlaywrightError as exc:
                log_event("login_cancelled", reason="browser_or_page_closed", error=str(exc))
            finally:
                if browser.is_connected():
                    browser.close()
        if connected:
            return {"status": "connected", "portal": self.portal_name, "storage_state": str(self.storage_state)}
        return {"status": "not_connected", "portal": self.portal_name, "reason": "Dice login was not verified."}

    def is_connected(self) -> bool:
        if not self.storage_state.exists():
            return False
        try:
            state = json.loads(self.storage_state.read_text(encoding="utf-8"))
        except Exception:
            return False
        return self._state_has_authenticated_session(state)

    def disconnect(self) -> None:
        self.storage_state.unlink(missing_ok=True)

    def search_jobs_many(
        self,
        resume: ResumeProfile,
        variations: list[SearchVariation],
        limit_per_query: int,
        progress_callback=None,
    ) -> list[tuple[SearchVariation, list[dict]]]:
        debug_dir = self._debug_dir()
        results: list[tuple[SearchVariation, list[dict]]] = []
        search_events: list[dict] = []
        if not self.storage_state.exists():
            self._write_debug(
                debug_dir,
                "zero_results_reason.json",
                {"portal": self.portal_name, "reason": "not_logged_in", "details": "Dice storage state is missing."},
            )
            return [(variation, []) for variation in variations]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=self.settings.linkedin_headless,
                slow_mo=self.settings.linkedin_slow_mo_ms,
            )
            context_kwargs = {"storage_state": str(self.storage_state)} if self.storage_state.exists() else {}
            context = browser.new_context(**context_kwargs)
            if not self._context_has_authenticated_session(context):
                self._write_debug(
                    debug_dir,
                    "zero_results_reason.json",
                    {
                        "portal": self.portal_name,
                        "reason": "not_logged_in",
                        "details": "Dice storage state exists but does not contain an authenticated session.",
                    },
                )
                browser.close()
                return [(variation, []) for variation in variations]
            page = context.new_page()
            for index, variation in enumerate(variations, start=1):
                raw_jobs = self.search_jobs(page, variation, limit_per_query, debug_dir=debug_dir, search_index=index)
                results.append((variation, raw_jobs))
                event = {
                    "portal": self.portal_name,
                    "search_index": index,
                    "query": variation.query,
                    "location": variation.location,
                    "jobs_found": len(raw_jobs),
                    "job_urls": [job.get("job_url") for job in raw_jobs],
                }
                search_events.append(event)
                self._write_debug(
                    debug_dir,
                    f"search_{index}.json",
                    {**event, "jobs": raw_jobs},
                )
                self._write_debug(debug_dir, "search_events.json", search_events)
                if progress_callback:
                    progress_callback(variation, raw_jobs, index, len(variations))
            browser.close()
        self._write_debug(
            debug_dir,
            "search_summary.json",
            {
                "portal": self.portal_name,
                "variation_count": len(variations),
                "jobs_found": sum(len(items) for _, items in results),
            },
        )
        return results

    def search_url(self, variation: SearchVariation, page_number: int = 1) -> str:
        query = quote_plus(variation.query)
        location = quote_plus(variation.location or "")
        return (
            "https://www.dice.com/jobs"
            f"?q={query}&location={location}&radius=30&radiusUnit=mi&page={page_number}&pageSize=20"
        )

    def search_jobs(
        self,
        page,
        variation: SearchVariation,
        limit: int,
        debug_dir: Path | None = None,
        search_index: int = 1,
    ) -> list[dict]:
        url = self.search_url(variation)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_timeout(2500)
        except Exception as exc:
            self._write_search_failure(
                debug_dir,
                search_index,
                "search_page_failed_to_load",
                {"url": url, "error": str(exc), "query": variation.query, "location": variation.location},
            )
            return []

        self._capture_search_artifacts(page, debug_dir, search_index, "loaded")
        blocker = self._detect_search_blocker(page)
        if blocker:
            self._write_search_failure(
                debug_dir,
                search_index,
                blocker,
                {"url": self._safe_page_url(page), "query": variation.query, "location": variation.location},
            )
            return []

        parsed_jobs = self._extract_jobs_from_dom(page, variation, limit)
        if parsed_jobs:
            return parsed_jobs

        fallback_jobs = self._extract_jobs_from_locators(page, variation, limit)
        if fallback_jobs:
            return fallback_jobs

        self._write_search_failure(
            debug_dir,
            search_index,
            "no_job_cards_found",
            {
                "url": self._safe_page_url(page),
                "query": variation.query,
                "location": variation.location,
                "selectors_tried": self.CARD_SELECTORS,
            },
        )
        return []

    def job_from_card_data(self, data: dict, variation: SearchVariation) -> dict:
        text = str(data.get("description") or data.get("text") or "")
        title = str(data.get("title") or "").strip() or self._first_line(text) or "Dice Job"
        company = str(data.get("company") or "").strip() or self._line_after(text, ["Company", "at"]) or "Unknown"
        location_text = (
            str(data.get("location") or "").strip()
            or self._line_matching(text, r"\b(Remote|Hybrid|[A-Z][a-z]+,\s*[A-Z]{2}|India|United States)\b")
            or variation.location
        )
        href = self._absolute_dice_url(str(data.get("job_url") or data.get("href") or ""))
        apply_url = self._absolute_dice_url(str(data.get("apply_url") or "")) or href
        posted_date = str(data.get("posted_date") or "").strip()
        easy_apply = bool(data.get("easy_apply")) or bool(re.search(r"\b(easy apply|apply now|quick apply)\b", text, re.I))
        return {
            "portal": self.portal_name,
            "title": title[:255],
            "company": company[:255],
            "location": location_text[:255] if location_text else variation.location,
            "job_url": href or self.search_url(variation),
            "description": text or " ".join(item for item in [title, company, location_text, posted_date] if item),
            "easy_apply": easy_apply,
            "form_provider": "dice",
            "job_description_metadata": {
                "portal": self.portal_name,
                "posted_date": posted_date,
                "apply_url": apply_url,
                "source_search_query": variation.query,
                "source_search_location": variation.location,
            },
        }

    def _extract_jobs_from_dom(self, page, variation: SearchVariation, limit: int) -> list[dict]:
        try:
            raw_cards = page.evaluate(
                """
                ({ cardSelectors, titleSelectors, companySelectors, locationSelectors, limit }) => {
                  const textOf = (root, selectors) => {
                    for (const selector of selectors) {
                      const element = root.querySelector(selector);
                      const text = (element && element.innerText || '').trim();
                      if (text) return text;
                    }
                    return '';
                  };
                  const hrefOf = (root, selectors) => {
                    for (const selector of selectors) {
                      const element = root.querySelector(selector);
                      const href = element && element.getAttribute && element.getAttribute('href');
                      if (href) return href;
                    }
                    return '';
                  };
                  const seen = new Set();
                  const cards = [];
                  for (const selector of cardSelectors) {
                    document.querySelectorAll(selector).forEach((card) => {
                      if (cards.length >= limit) return;
                      if (seen.has(card)) return;
                      const text = (card.innerText || '').trim();
                      const jobUrl = hrefOf(card, [
                        "a[href*='/job-detail/']",
                        "a[href*='/jobs/detail/']",
                        "a[href*='/job/']",
                        "a[href*='dice.com/job']"
                      ]);
                      if (!text && !jobUrl) return;
                      seen.add(card);
                      const applyLink = hrefOf(card, [
                        "a[href*='apply']"
                      ]);
                      const postedMatch = text.match(/(?:posted\\s*)?(?:today|just now|\\d+\\s+(?:hours?|days?|weeks?|months?)\\s+ago)/i);
                      cards.push({
                        title: textOf(card, titleSelectors),
                        company: textOf(card, companySelectors),
                        location: textOf(card, locationSelectors),
                        job_url: jobUrl,
                        apply_url: applyLink,
                        posted_date: postedMatch ? postedMatch[0] : '',
                        description: text,
                        easy_apply: /easy apply|quick apply|apply now/i.test(text)
                      });
                    });
                    if (cards.length >= limit) break;
                  }
                  return cards.slice(0, limit);
                }
                """,
                {
                    "cardSelectors": self.CARD_SELECTORS,
                    "titleSelectors": self.TITLE_SELECTORS,
                    "companySelectors": self.COMPANY_SELECTORS,
                    "locationSelectors": self.LOCATION_SELECTORS,
                    "limit": limit,
                },
            )
        except Exception:
            return []
        jobs: list[dict] = []
        for item in raw_cards or []:
            try:
                job = self.job_from_card_data(item, variation)
            except Exception:
                continue
            if job["title"] and job["company"] and job["job_url"]:
                jobs.append(job)
        return jobs[:limit]

    def _extract_jobs_from_locators(self, page, variation: SearchVariation, limit: int) -> list[dict]:
        cards = page.locator(", ".join(self.CARD_SELECTORS))
        count = min(cards.count(), limit)
        jobs: list[dict] = []
        for index in range(count):
            card = cards.nth(index)
            text = self._safe_text(card)
            if not text.strip():
                continue
            link = card.locator("a[href*='/job-detail/'], a[href*='/jobs/detail/'], a[href*='dice.com/job']").first
            href = self._safe_attr(link, "href")
            jobs.append(self.job_from_card_text(text, href or self.search_url(variation), variation))
        return jobs

    def job_from_card_text(self, text: str, href: str, variation: SearchVariation) -> dict:
        title = self._first_line(text) or "Dice Job"
        company = self._line_after(text, ["Company", "at"]) or "Unknown"
        location_text = self._line_matching(text, r"\b(Remote|Hybrid|[A-Z][a-z]+,\s*[A-Z]{2}|India|United States)\b") or variation.location
        easy_apply = bool(re.search(r"\b(easy apply|apply now|quick apply)\b", text, re.I))
        return {
            "portal": self.portal_name,
            "title": title[:255],
            "company": company[:255],
            "location": location_text,
            "job_url": href,
            "description": text,
            "easy_apply": easy_apply,
            "form_provider": "dice",
            "job_description_metadata": {
                "portal": self.portal_name,
                "apply_url": href,
                "source_search_query": variation.query,
                "source_search_location": variation.location,
            },
        }

    def apply_to_job(
        self,
        job: JobOpportunity,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        *,
        dry_run: bool,
        capture_evidence: bool,
        screening_context: dict | None = None,
    ) -> AutomationResult:
        """Dice dry-run application (safe): fills the apply form using the shared
        ScreeningAnswerEngine + GeneralizedFormEngine but NEVER clicks final submit.

        Outcomes:
          - dry_run_ready     : form filled to the submit-ready stage (not submitted)
          - unsupported_form  : external ATS / no native form / unhandled controls
          - needs_review      : a required answer is uncertain or profile data missing
        """
        debug_dir = self._debug_dir() / f"job_{job.id}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        screening_context = screening_context or {}
        log: list[dict] = [
            {"step": "apply_started", "portal": self.portal_name, "job_url": job.job_url, "dry_run": dry_run}
        ]

        # Live apply is intentionally not enabled for Dice: never submit.
        if not dry_run:
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW,
                "Live apply is disabled for Dice; dry-run only (no submission performed).",
                "dice_live_apply_disabled",
                debug_dir,
                job,
                log=log,
            )

        try:
            with self._apply_browser_context() as context:
                page = context.new_page()
                try:
                    page.goto(self._apply_target_url(job), wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:  # navigation failure is non-fatal for classification
                    log.append({"step": "job_open_failed", "error": str(exc)})
                form = self._detect_apply_form(page, job)
                log.append({"step": "apply_form_detected", "apply_type": form.get("apply_type"), "field_count": len(form.get("fields") or [])})
                result = self._plan_dry_run(page, form, job, resume, candidate_profile, screening_context, debug_dir, log)
                if capture_evidence:
                    self._capture_apply_artifact(page, debug_dir)
                return result
        except PlaywrightError as exc:
            return self._dice_result(
                ApplicationStatus.FAILED,
                f"Dice apply could not launch a browser session: {exc}",
                "dice_apply_launch_failed",
                debug_dir,
                job,
                log=log,
            )

    # ---- Apply-flow seams (overridable for tests; best-effort for live Dice) ----
    @contextmanager
    def _apply_browser_context(self):
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=self.settings.linkedin_headless,
            slow_mo=self.settings.linkedin_slow_mo_ms,
        )
        kwargs = {"storage_state": str(self.storage_state)} if self.storage_state.exists() else {}
        context = browser.new_context(**kwargs)
        try:
            yield context
        finally:
            try:
                context.close()
            finally:
                browser.close()
                playwright.stop()

    def _apply_target_url(self, job: JobOpportunity) -> str:
        meta = job.job_description_metadata or {}
        return meta.get("apply_url") or job.job_url

    def _detect_apply_form(self, page, job: JobOpportunity) -> dict:
        """Return {apply_type, fields, submit_present}. Best-effort against live Dice.

        apply_type: 'native' (Dice easy-apply form), 'external' (redirects to an ATS),
        or 'none' (no apply form found). Tests override this seam with canned data.
        """
        if self._has_external_apply(page):
            return {"apply_type": "external", "fields": [], "submit_present": False}
        if not self._open_native_apply(page):
            return {"apply_type": "none", "fields": [], "submit_present": False}
        return {
            "apply_type": "native",
            "fields": self._collect_fields(page),
            "submit_present": self._has_submit(page),
        }

    def _has_external_apply(self, page) -> bool:
        for selector in (
            "a:has-text('Apply on company site')",
            "a:has-text('Apply on employer site')",
            "[data-cy='external-apply']",
        ):
            try:
                if page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _open_native_apply(self, page) -> bool:
        for selector in (
            "[data-cy='easy-apply-button']",
            "button:has-text('Easy apply')",
            "button:has-text('Easy Apply')",
            "button:has-text('Apply now')",
        ):
            try:
                button = page.locator(selector).first
                if button.count() > 0:
                    button.click(timeout=4000)
                    page.wait_for_timeout(500)
                    return True
            except Exception:
                continue
        return False

    def _collect_fields(self, page) -> list[dict]:
        try:
            return page.evaluate(self._collect_fields_script()) or []
        except Exception:
            return []

    def _has_submit(self, page) -> bool:
        for selector in (
            "button:has-text('Submit')",
            "[data-cy='submit-application']",
            "button[type='submit']",
        ):
            try:
                if page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _fill_fields(self, page, plan: list[dict]) -> None:
        """Fill planned values in the live form (dry-run). NEVER submits."""
        fillable = [entry for entry in plan if entry.get("value") is not None and entry.get("action") == "fill"]
        if not fillable:
            return
        try:
            page.evaluate(self._fill_fields_script(), fillable)
        except Exception:
            pass

    def _capture_apply_artifact(self, page, debug_dir: Path) -> None:
        try:
            page.screenshot(path=str(debug_dir / "apply_form.png"), full_page=True)
        except Exception:
            pass

    # ---- Dry-run planning / decisions (portal-agnostic, fully unit-tested) ----
    def _plan_dry_run(
        self,
        page,
        form: dict,
        job: JobOpportunity,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
        debug_dir: Path,
        log: list[dict],
    ) -> AutomationResult:
        apply_type = form.get("apply_type")
        if apply_type in {"external", "none"}:
            reason = (
                "Dice apply redirects to an external ATS; not automated."
                if apply_type == "external"
                else "No Dice native apply form was found for this job."
            )
            log.append({"step": "apply_unsupported", "apply_type": apply_type, "reason": reason})
            return self._dice_result(ApplicationStatus.UNSUPPORTED_FORM, reason, "dice_apply_unsupported_form", debug_dir, job, log=log)

        fields = form.get("fields") or []
        plan: list[dict] = []
        screening_answers: list[dict] = []
        missing_contact: list[str] = []
        unanswered: list[dict] = []
        unsupported_controls: list[dict] = []

        for field in fields:
            entry = self._field_plan_entry(field, resume, job, candidate_profile, screening_context)
            plan.append(entry)
            if entry["kind"] == "screening" and entry.get("event"):
                screening_answers.append(entry["event"])
            if entry["kind"] == "contact" and entry.get("value") is None and field.get("required"):
                missing_contact.append(entry["normalized_field"])
            if entry.get("action") == "needs_review":
                unanswered.append({"label": entry["label"], "reason": entry["reason"]})
            if entry.get("action") == "unsupported":
                unsupported_controls.append({"label": entry["label"], "control": entry["control"]})

        self._write_debug(
            debug_dir,
            "dry_run_plan.json",
            {
                "portal": self.portal_name,
                "job_id": job.id,
                "apply_type": apply_type,
                "submit_present": form.get("submit_present"),
                "plan": plan,
                "unanswered": unanswered,
                "unsupported_controls": unsupported_controls,
                "missing_contact_fields": missing_contact,
            },
        )

        if unanswered:
            reason = "Dice dry run stopped: one or more required answers were uncertain (needs review)."
            log.append({"step": "needs_review", "unanswered": unanswered})
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW, reason, "dice_apply_needs_review", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )
        if unsupported_controls:
            reason = "Dice form contains controls this automation does not yet support."
            log.append({"step": "unsupported_controls", "controls": unsupported_controls})
            return self._dice_result(
                ApplicationStatus.UNSUPPORTED_FORM, reason, "dice_apply_unsupported_form", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )

        # All required fields are answerable: fill everything EXCEPT final submit.
        self._fill_fields(page, plan)
        log.append({"step": "dry_run_filled", "fields_planned": len(plan), "submitted": False})
        reason = "Dice dry run reached the submit-ready stage; application was NOT submitted."
        return self._dice_result(
            ApplicationStatus.DRY_RUN_READY, reason, "dice_dry_run_ready", debug_dir, job,
            log=log, screening_answers=screening_answers, missing_contact=missing_contact,
        )

    def _field_plan_entry(
        self,
        field: dict,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
    ) -> dict:
        label = str(field.get("label") or "").strip()
        options = list(field.get("options") or [])
        control = str(field.get("control") or field.get("kind") or "text").lower()
        kind = str(field.get("kind") or control).lower()
        required = bool(field.get("required"))

        # Control-type detection via the shared GeneralizedFormEngine.
        detection = self.form_engine.detect_field(
            GeneralizedFieldInput(
                label=label or "Unknown Field",
                input_type=kind,
                options=options,
                required=required,
                portal="dice",
            )
        )
        base = {
            "index": field.get("index"),
            "label": label,
            "control": control,
            "kind": kind,
            "required": required,
            "options": options,
            "normalized_field": detection.normalized_field_name,
            "detected_control_type": detection.detected_control_type,
        }

        # 1) Resume upload
        if detection.detected_control_type == "file_upload" or re.search(r"resume|cv|upload", label, re.I):
            return {**base, "kind": "resume_upload", "action": "resume_upload", "value": None,
                    "reason": "Resume upload planned (not performed in dry run)."}

        # 2) Candidate contact identity fields (from saved profile)
        contact_attr = self._contact_attr_for_label(label)
        if contact_attr:
            value = getattr(candidate_profile, contact_attr, None) if candidate_profile else None
            value = (value or "").strip() or None
            return {**base, "kind": "contact", "action": "fill" if value else ("needs_review" if required else "skip"),
                    "value": value, "normalized_field": contact_attr,
                    "reason": None if value else f"Candidate profile is missing '{contact_attr}'."}

        # 3) Screening questions -> shared ScreeningAnswerEngine
        decision = self.screening.decide(
            ScreeningQuestion(text=label, field_type=control, field_kind=kind, options=options, portal="dice"),
            resume=resume,
            job=job,
            candidate_profile=candidate_profile,
            context=screening_context,
        )
        event = decision.to_event() or {
            "question_text": label,
            "selected_answer": None,
            "source": None,
            "confidence": decision.confidence,
            "reason": decision.reasoning or "No confident answer; needs review.",
            "category": decision.category,
        }
        event.setdefault("question_text", label)
        event.setdefault("category", decision.category)
        if decision.needs_review or decision.answer is None:
            if required:
                return {**base, "kind": "screening", "action": "needs_review", "value": None,
                        "reason": "Screening answer was uncertain.", "event": event}
            return {**base, "kind": "screening", "action": "skip", "value": None,
                    "reason": "Optional screening question left blank.", "event": event}
        return {**base, "kind": "screening", "action": "fill", "value": decision.answer, "event": event}

    def _contact_attr_for_label(self, label: str) -> str | None:
        lowered = (label or "").lower()
        for attr, pattern in self.CONTACT_FIELD_PATTERNS:
            if re.search(pattern, lowered):
                return attr
        return None

    def _dice_result(
        self,
        status: ApplicationStatus,
        message: str,
        current_step: str,
        debug_dir: Path,
        job: JobOpportunity,
        *,
        log: list[dict] | None = None,
        screening_answers: list[dict] | None = None,
        missing_contact: list[str] | None = None,
    ) -> AutomationResult:
        log = list(log or [])
        log.append({"step": "apply_result", "status": status.value, "current_step": current_step})
        self._write_debug(
            debug_dir,
            "apply_attempt.json",
            {
                "portal": self.portal_name,
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "job_url": job.job_url,
                "status": status.value,
                "current_step": current_step,
                "message": message,
                "submitted": False,
                "screening_answers": screening_answers or [],
                "missing_contact_fields": missing_contact or [],
            },
        )
        return AutomationResult(
            status=status,
            message=message,
            current_step=current_step,
            evidence_path=str(debug_dir),
            log=log,
            screening_answers=screening_answers or [],
            missing_contact_fields=missing_contact or [],
        )

    def _collect_fields_script(self) -> str:
        return """
            () => {
                const forms = document.querySelectorAll(
                    "[data-cy='easy-apply-form'], form[action*='apply'], .apply-form, [class*='apply']"
                );
                const root = forms[0] || document;
                const controls = Array.from(root.querySelectorAll('input, textarea, select'))
                    .filter(el => {
                        const type = (el.type || '').toLowerCase();
                        if (['hidden', 'submit', 'button'].includes(type)) return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    });
                const seenGroups = new Set();
                const fields = [];
                const labelFor = el => {
                    const id = el.id;
                    const byFor = id ? document.querySelector(`label[for="${id}"]`) : null;
                    const wrap = el.closest('label');
                    const group = el.closest('fieldset, [role="group"], .form-group, [class*="field"]');
                    const legend = group ? group.querySelector('legend, label, .label') : null;
                    return String((byFor && byFor.innerText) || (wrap && wrap.innerText) || (legend && legend.innerText) || el.getAttribute('aria-label') || el.name || el.placeholder || '').replace(/\\s+/g, ' ').trim();
                };
                controls.forEach((el, index) => {
                    const tag = el.tagName.toLowerCase();
                    const type = (el.type || tag).toLowerCase();
                    if (type === 'radio' || type === 'checkbox') {
                        const name = el.getAttribute('name') || '';
                        const key = name || labelFor(el);
                        if (key && seenGroups.has(key)) return;
                        if (key) seenGroups.add(key);
                        const groupEls = name ? Array.from(root.querySelectorAll(`input[name="${name}"]`)) : [el];
                        fields.push({
                            index,
                            label: labelFor(el),
                            control: type,
                            kind: type,
                            required: groupEls.some(g => g.required),
                            options: groupEls.map(g => labelFor(g) || g.value).filter(Boolean),
                        });
                        return;
                    }
                    let options = [];
                    if (tag === 'select') options = Array.from(el.options).map(o => (o.textContent || o.value || '').trim()).filter(Boolean);
                    fields.push({
                        index,
                        label: labelFor(el),
                        control: tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : type),
                        kind: tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : 'text'),
                        required: Boolean(el.required),
                        options,
                    });
                });
                return fields;
            }
        """

    def _fill_fields_script(self) -> str:
        return """
            (plan) => {
                // Dry-run fill only. This function never submits the form.
                const controls = Array.from(document.querySelectorAll('input, textarea, select'));
                for (const entry of plan) {
                    const el = controls[entry.index];
                    if (!el) continue;
                    const tag = el.tagName.toLowerCase();
                    const type = (el.type || tag).toLowerCase();
                    try {
                        if (type === 'radio' || type === 'checkbox') {
                            el.checked = true;
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        } else if (tag === 'select') {
                            el.value = entry.value;
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        } else {
                            el.value = entry.value;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    } catch (_) { /* ignore individual field errors in dry run */ }
                }
                return true;
            }
        """

    def _safe_text(self, locator) -> str:
        try:
            return locator.inner_text(timeout=1500)
        except Exception:
            return ""

    def _safe_attr(self, locator, attr: str) -> str | None:
        try:
            value = locator.get_attribute(attr, timeout=1000)
        except Exception:
            return None
        if value and value.startswith("/"):
            return f"https://www.dice.com{value}"
        return value

    def _safe_page_url(self, page) -> str:
        try:
            return str(page.url)
        except Exception:
            return ""

    def _absolute_dice_url(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        return urljoin("https://www.dice.com", value)

    def _capture_search_artifacts(self, page, debug_dir: Path | None, search_index: int, suffix: str) -> None:
        if debug_dir is None:
            return
        try:
            page.screenshot(path=str(debug_dir / f"search_{search_index}_{suffix}.png"), full_page=True)
        except Exception:
            pass
        try:
            (debug_dir / f"search_{search_index}_{suffix}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass

    def _write_search_failure(self, debug_dir: Path | None, search_index: int, reason: str, payload: dict) -> None:
        if debug_dir is None:
            return
        self._write_debug(
            debug_dir,
            f"search_{search_index}_zero_result.json",
            {"portal": self.portal_name, "reason": reason, **payload},
        )

    def _detect_search_blocker(self, page) -> str | None:
        try:
            text = page.locator("body").inner_text(timeout=1500).lower()
        except Exception:
            text = ""
        url = self._safe_page_url(page).lower()
        if "login" in url or "sign-in" in url or "signin" in url:
            return "not_logged_in"
        if any(marker in text for marker in ["captcha", "verify you are human", "access denied", "unusual traffic"]):
            return "captcha_or_blocked"
        if any(marker in text for marker in ["no jobs found", "0 jobs", "no results found"]):
            return "no_results_returned_by_dice"
        return None

    def _first_line(self, text: str) -> str | None:
        for line in (item.strip() for item in text.splitlines()):
            if line:
                return line
        return None

    def _line_after(self, text: str, markers: list[str]) -> str | None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines[:-1]):
            normalized = line.lower()
            for marker in markers:
                marker_text = marker.lower()
                if normalized == marker_text or re.search(rf"\b{re.escape(marker_text)}\b", normalized):
                    return lines[index + 1]
        return None

    def _line_matching(self, text: str, pattern: str) -> str | None:
        for line in (item.strip() for item in text.splitlines()):
            if re.search(pattern, line):
                return line
        return None

    def _page_looks_logged_in(self, page) -> bool:
        try:
            url = page.url.lower()
        except Exception:
            return False
        if re.search(r"/(login|signin|sign-in|register|registration)", url):
            return False
        if re.search(r"dice\.com/(dashboard|profile|home|candidate|jobs/saved|dashboard/)", url):
            return True
        selectors = [
            "[data-cy='profile-menu']",
            "[data-cy='user-menu']",
            "a[href*='/dashboard/profile']",
            "a[href*='/profile']",
            "button:has-text('Profile')",
            "text=My Profile",
            "text=Dashboard",
        ]
        for selector in selectors:
            try:
                if page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _context_has_authenticated_session(self, context) -> bool:
        try:
            state = context.storage_state()
        except Exception:
            return False
        return self._state_has_authenticated_session(state)

    def _state_has_authenticated_session(self, state: dict) -> bool:
        cookies = state.get("cookies") or []
        if any(self._is_authenticated_cookie(cookie) for cookie in cookies):
            return True
        for origin in state.get("origins") or []:
            if "dice.com" not in str(origin.get("origin") or ""):
                continue
            for item in origin.get("localStorage") or []:
                key = str(item.get("name") or "")
                value = str(item.get("value") or "")
                if self.AUTH_STORAGE_RE.search(key) and value and value.lower() not in {"null", "undefined", "false"}:
                    return True
        return False

    def _is_authenticated_cookie(self, cookie: dict) -> bool:
        domain = str(cookie.get("domain") or "")
        if "dice.com" not in domain:
            return False
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name or not value:
            return False
        if self.ANONYMOUS_COOKIE_RE.search(name):
            return False
        return bool(self.AUTH_COOKIE_RE.search(name))
