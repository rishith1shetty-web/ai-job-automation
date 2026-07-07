from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from app.config import Settings
from app.models import ApplicationStatus, CandidateProfile, JobOpportunity, ResumeProfile
from app.services.find_matches import SearchVariation
from app.services.linkedin import AutomationResult


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

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def storage_state(self) -> Path:
        return self.settings.dice_storage_state

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
        debug_dir = self._debug_dir() / f"job_{job.id}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "portal": self.portal_name,
            "job_id": job.id,
            "title": job.title,
            "company": job.company,
            "job_url": job.job_url,
            "dry_run": dry_run,
            "status": "needs_review",
            "reason": "Dice apply automation is in Phase 1 skeleton mode; manual review required for this portal form.",
        }
        self._write_debug(debug_dir, "apply_attempt.json", payload)
        return AutomationResult(
            status=ApplicationStatus.NEEDS_REVIEW,
            message=payload["reason"],
            current_step="dice_apply_unsupported",
            evidence_path=str(debug_dir),
            log=[
                {"step": "job_opened", "status": "not_started", "portal": self.portal_name, "job_url": job.job_url},
                {"step": "apply_supported", "status": "needs_review", "reason": payload["reason"]},
            ],
        )

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
