from __future__ import annotations

import inspect
import json
import re
import time
import traceback
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

    # --- Native-vs-external apply classification (Find Matches keeps native only) ---
    # Native Dice one-click application CTAs. Everything else -- a bare "Apply" /
    # "Apply now" button or an external ATS -- is filtered out of Find Matches so
    # ranking and Apply Top N only ever see automatable jobs, mirroring LinkedIn's
    # Easy Apply-only search (f_AL=true). To go ultra-strict (literally only
    # "Easy Apply"), drop "quick\s*apply" from this one pattern.
    NATIVE_APPLY_CTA_RE = re.compile(r"\b(easy\s*apply|quick\s*apply)\b", re.I)
    # Explicit "apply on company site" style CTA phrasing (matched against the CTA
    # and card text). Deliberately requires the word "apply" so a description that
    # merely says "company website" is not wrongly excluded.
    EXTERNAL_CTA_RE = re.compile(
        r"apply\s+on\s+(the\s+)?(company|employer)('s)?\s+(site|website)"
        r"|apply\s+externally|external\s+apply|apply\s+on\s+company\s+site"
        r"|apply\s+on\s+employer\s+site|apply\s+via\s+company",
        re.I,
    )
    # External applicant-tracking systems: matched ONLY against the apply URL/host
    # and the short CTA text -- never the free-form description -- so a JD that
    # merely mentions "Workday" as a tool is not wrongly excluded.
    EXTERNAL_ATS_HOST_MARKERS = [
        ("workday", r"workday|myworkdayjobs"),
        ("greenhouse", r"greenhouse"),
        ("lever", r"lever\.co|jobs\.lever"),
        ("icims", r"icims"),
        ("taleo", r"taleo"),
        ("smartrecruiters", r"smartrecruiters"),
        ("jobvite", r"jobvite"),
        ("brassring", r"brassring"),
        ("ashby", r"ashbyhq"),
        ("workable", r"workable\.com"),
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

    # ---- Full wizard instrumentation -------------------------------------------
    # Every branch that can end a Dice apply run records WHY, and at which source
    # line, so a browser close is never unexplained. Events land in
    # <debug_dir>/apply_trace.json and are mirrored into the AutomationResult log.

    def _caller_loc(self, depth: int = 2) -> str:
        """'dice_portal.py:1234 in _click_next' for the frame `depth` levels up."""
        try:
            frame = inspect.currentframe()
            for _ in range(depth):
                frame = frame.f_back
            return f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno} in {frame.f_code.co_name}"
        except Exception:
            return "unknown"

    def _trace(self, event: str, **fields) -> dict:
        """Record one instrumentation event, tagged with its exact source line."""
        entry = {
            "event": event,
            "at": self._caller_loc(depth=2),
            "time": datetime.utcnow().isoformat(),
            "wizard_step": getattr(self, "_wizard_last_step", None),
            "step_name": getattr(self, "_current_step_name", None),
            **fields,
        }
        events = getattr(self, "_wizard_events", None)
        if events is None:
            events = self._wizard_events = []
        events.append(entry)
        return entry

    def _trace_return(self, where: str, reason: str, log: list[dict] | None = None,
                      include_stack: bool = False, **fields) -> None:
        """Record an explicit return path. `where` is the function returning."""
        entry = self._trace("return_path", where=where, reason=reason, **fields)
        entry["at"] = self._caller_loc(depth=2)
        if include_stack:
            entry["stack"] = [line.rstrip() for line in traceback.format_stack(limit=12)[:-1]]
        if log is not None:
            log.append({"step": "return_path", "where": where, "reason": reason,
                        "at": entry["at"], **fields})

    def _write_apply_trace(self) -> None:
        debug_dir = getattr(self, "_active_debug_dir", None)
        if debug_dir is None:
            return
        try:
            self._write_debug(
                Path(debug_dir),
                "apply_trace.json",
                {
                    "portal": self.portal_name,
                    "last_step": getattr(self, "_wizard_last_step", None),
                    "last_step_name": getattr(self, "_current_step_name", None),
                    "last_url": getattr(self, "_wizard_last_url", None),
                    "stop_code": getattr(self, "_wizard_stop_code", None),
                    "stop_reason": getattr(self, "_wizard_stop_reason", None),
                    "stop_at": getattr(self, "_wizard_stop_at", None),
                    "final_status": getattr(self, "_final_status", None),
                    "events": getattr(self, "_wizard_events", []),
                },
            )
        except Exception:
            pass

    def _safe_eval(self, page, script, expect, default):
        """page.evaluate() that tolerates fakes/stubs returning the wrong shape."""
        try:
            value = page.evaluate(script)
        except Exception:
            return default
        return value if isinstance(value, expect) else default

    def _progress_text(self, page) -> str:
        """The wizard's step/progress indicator text ('Step 1 of 3', breadcrumbs)."""
        return self._safe_eval(
            page,
            """() => Array.from(document.querySelectorAll(
                    "[role='progressbar'], [aria-current='step'], nav[aria-label*='progress' i], ol li, [class*='step' i]"))
                .map(el => ((el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean).slice(0, 8).join(' | ').slice(0, 300)""",
            str, "",
        )

    def _dom_fingerprint(self, page) -> str:
        """Cheap DOM signature used to answer 'did the DOM change after Next?'."""
        return self._safe_eval(
            page,
            """() => {
                const b = document.body;
                if (!b) return '';
                const controls = document.querySelectorAll('input, select, textarea, button');
                return (b.innerHTML || '').length + ':' + controls.length + ':' + location.pathname;
            }""",
            str, "",
        )

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
        # easyApply mirrors LinkedIn's f_AL=true: ask Dice's own search to only
        # return Easy Apply jobs (client-side filtering in _filter_easy_apply_only
        # still applies as a guarantee if Dice ignores/renames this param).
        return (
            "https://www.dice.com/jobs"
            f"?q={query}&location={location}&radius=30&radiusUnit=mi&page={page_number}&pageSize=20&easyApply=true"
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
            return self._filter_easy_apply_only(parsed_jobs, debug_dir, search_index)

        fallback_jobs = self._extract_jobs_from_locators(page, variation, limit)
        if fallback_jobs:
            return self._filter_easy_apply_only(fallback_jobs, debug_dir, search_index)

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

    def _classify_dice_apply(self, *, card_text: str, cta_text: str, apply_url: str) -> tuple[bool, str]:
        """Return (is_native_easy_apply, reason).

        Native = a Dice one-click CTA (Easy Apply / Quick Apply) that stays on
        Dice. Everything else -- a bare 'Apply' / 'Apply now' button or any
        external ATS (Workday, Greenhouse, Lever, company website, ...) -- is
        excluded so Find Matches returns only automatable jobs.
        """
        cta = re.sub(r"\s+", " ", (cta_text or "")).strip()
        url = (apply_url or "").lower()

        # 1) External ATS by apply-URL host or short CTA text (not the description).
        ats_haystack = f"{url} {cta.lower()}"
        for name, pattern in self.EXTERNAL_ATS_HOST_MARKERS:
            if re.search(pattern, ats_haystack, re.I):
                return False, f"external_ats:{name}"

        # 2) Explicit "apply on company site" style CTA phrasing.
        if self.EXTERNAL_CTA_RE.search(f"{cta} {card_text or ''}"):
            return False, "external_cta"

        # 3) When a CTA was captured, it must itself be a native Easy/Quick Apply.
        if cta:
            if self.NATIVE_APPLY_CTA_RE.search(cta):
                return True, f"native_cta:{cta.lower()[:40]}"
            return False, f"non_native_cta:{cta.lower()[:40]}"

        # 4) No CTA captured -> fall back to a strict native badge in the card text.
        if self.NATIVE_APPLY_CTA_RE.search(card_text or ""):
            return True, "native_badge_text"
        return False, "no_easy_apply_signal"

    def _filter_easy_apply_only(
        self, jobs: list[dict], debug_dir: Path | None, search_index: int
    ) -> list[dict]:
        """Find Matches returns ONLY native Dice Easy Apply jobs, mirroring
        LinkedIn's Easy Apply-only search. External/plain-Apply jobs are dropped
        here so ranking and Apply Top N never operate on them."""
        kept: list[dict] = []
        excluded: list[dict] = []
        for job in jobs:
            meta = job.get("job_description_metadata") or {}
            if job.get("easy_apply"):
                kept.append(job)
            else:
                excluded.append(
                    {
                        "title": job.get("title"),
                        "company": job.get("company"),
                        "job_url": job.get("job_url"),
                        "apply_cta": meta.get("apply_cta"),
                        "reason": meta.get("apply_classification") or "not_easy_apply",
                    }
                )
        if debug_dir is not None:
            self._write_debug(
                debug_dir,
                f"search_{search_index}_easy_apply_filter.json",
                {
                    "portal": self.portal_name,
                    "total_jobs_found": len(jobs),
                    "easy_apply_kept": len(kept),
                    "external_or_apply_filtered": len(excluded),
                    "excluded_jobs": excluded,
                },
            )
        return kept

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
        # Keep the raw (possibly external) apply URL for classification before the
        # dice.com fallback, so an external-ATS host is still detectable.
        raw_apply_url = str(data.get("apply_url") or "")
        apply_url = self._absolute_dice_url(raw_apply_url) or href
        posted_date = str(data.get("posted_date") or "").strip()
        apply_cta = str(data.get("apply_cta") or "").strip()
        easy_apply, apply_reason = self._classify_dice_apply(
            card_text=text, cta_text=apply_cta, apply_url=raw_apply_url or apply_url
        )
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
                "apply_cta": apply_cta,
                "apply_classification": apply_reason,
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
                      // Capture the actual apply CTA / badge text (short, button-like),
                      // NOT a loose match over the whole card text. Python then decides
                      // native-vs-external. Prefer dedicated apply chips/ribbons first.
                      const applyCta = (() => {
                        const ctaEls = card.querySelectorAll(
                          "[data-cy*='easyApply' i], [data-cy*='apply' i], [class*='easyApply' i], "
                          + "[class*='ribbon' i], [class*='badge' i], [class*='chip' i], "
                          + "button, a[href*='apply' i], [role='button']"
                        );
                        for (const el of ctaEls) {
                          const t = ((el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
                          if (t && /apply/i.test(t) && t.length <= 40) return t;
                        }
                        return '';
                      })();
                      const postedMatch = text.match(/(?:posted\\s*)?(?:today|just now|\\d+\\s+(?:hours?|days?|weeks?|months?)\\s+ago)/i);
                      cards.push({
                        title: textOf(card, titleSelectors),
                        company: textOf(card, companySelectors),
                        location: textOf(card, locationSelectors),
                        job_url: jobUrl,
                        apply_url: applyLink,
                        apply_cta: applyCta,
                        posted_date: postedMatch ? postedMatch[0] : '',
                        description: text
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
        # No isolated CTA in the locator fallback -> classify from the card text
        # (a native Easy/Quick Apply badge line) plus the job-detail href.
        easy_apply, apply_reason = self._classify_dice_apply(card_text=text, cta_text="", apply_url=href)
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
                "apply_cta": "",
                "apply_classification": apply_reason,
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

        # Apply context shared with the step-wise wizard executor (which runs inside
        # _detect_apply_form/_gather_form_fields and therefore can't take these as
        # params without changing seam signatures that tests override).
        self._active_debug_dir = debug_dir
        self._active_job = job
        self._active_resume = resume
        self._active_candidate = candidate_profile
        self._active_screening_context = screening_context
        # Wizard lifecycle bookkeeping (why/where the run stopped).
        self._wizard_stop_code = None
        self._wizard_stop_reason = None
        self._wizard_last_url = None
        self._wizard_last_step = 0
        self._current_step_name = None
        self._wizard_events = []
        self._wizard_stop_at = None
        self._final_status = None
        self._review_missing_required = None
        self._city_commit_failure = None
        self._trace("apply_started", job_id=job.id, job_url=job.job_url, dry_run=dry_run)
        # Live apply is enabled for Dice. dry_run=False reaches the real submit path in
        # _plan_dry_run, which clicks Submit ONLY after every existing safety check has passed
        # and returns APPLIED only once submission is positively confirmed.
        self._dry_run = dry_run

        try:
            with self._apply_browser_context() as context:
                self._trace("browser_context_opened")
                page = context.new_page()
                try:
                    page.goto(self._apply_target_url(job), wait_until="domcontentloaded", timeout=60000)
                    log.append({"step": "job_opened", "url": self._safe_page_url(page)})
                    self._trace("job_opened", url=self._safe_page_url(page))
                    # Consent banner can cover the Apply/Next controls: clear it and
                    # wait for it to disappear before any automation runs.
                    self._handle_cookie_banner(page, log)
                except Exception as exc:  # navigation failure is non-fatal for classification
                    log.append({"step": "job_open_failed", "error": str(exc)})
                    self._trace("job_open_failed", error=str(exc), error_type=type(exc).__name__)
                form = self._detect_apply_form(page, job)
                # If clicking Apply opened a new tab (Dice sometimes routes the
                # application through a popup rather than an in-page modal),
                # _detect_apply_form reports the popup as the active page here.
                active_page = form.get("page") or page
                log.extend(form.get("detection_log") or [])
                log.append({
                    "step": "apply_form_detected",
                    "apply_type": form.get("apply_type"),
                    "field_count": len(form.get("fields") or []),
                    "opened_new_tab": active_page is not page,
                })
                self._trace("apply_form_detected", apply_type=form.get("apply_type"),
                            field_count=len(form.get("fields") or []),
                            opened_new_tab=active_page is not page)
                result = self._plan_dry_run(active_page, form, job, resume, candidate_profile,
                                            screening_context, debug_dir, log, dry_run=dry_run)
                if capture_evidence:
                    self._capture_apply_artifact(active_page, debug_dir)
                self._final_status = result.status.value
                self._trace_return("apply_to_job", f"_plan_dry_run returned {result.status.value}", log,
                                   status=result.status.value, current_step=result.current_step,
                                   stop_code=getattr(self, "_wizard_stop_code", None),
                                   stop_at=getattr(self, "_wizard_stop_at", None))
                # The context manager below closes the browser on the next line;
                # by here the reason is always recorded.
                self._trace("browser_context_closing", url=self._safe_page_url(active_page),
                            reason=getattr(self, "_wizard_stop_reason", None) or result.message)
                return result
        except PlaywrightError as exc:
            self._final_status = "failed"
            self._trace_return("apply_to_job", f"PlaywrightError: {exc}", log, include_stack=True,
                               error_type=type(exc).__name__)
            return self._dice_result(
                ApplicationStatus.FAILED,
                f"Dice apply could not launch a browser session: {exc}",
                "dice_apply_launch_failed",
                debug_dir,
                job,
                log=log,
            )
        except Exception as exc:  # never leave a job unclassified / silently skipped
            log.append({"step": "apply_exception", "error": str(exc),
                        "last_url": getattr(self, "_wizard_last_url", None),
                        "last_step": getattr(self, "_wizard_last_step", None)})
            self._final_status = "failed"
            self._trace_return("apply_to_job", f"unhandled {type(exc).__name__}: {exc}", log,
                               include_stack=True, error_type=type(exc).__name__,
                               traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:])
            return self._dice_result(
                ApplicationStatus.FAILED,
                f"Dice apply stopped on an unexpected error ({type(exc).__name__}): {exc}",
                "dice_apply_failed",
                debug_dir,
                job,
                log=log,
            )
        finally:
            self._write_apply_trace()

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
        """Return {apply_type, fields, submit_present, detection_log, page}. Best-effort
        against live Dice. `detection_log` carries step-by-step diagnostics
        (button search, form open, fields detected) merged into apply_to_job's log.
        `page` is the active page fields were collected from -- clicking Apply can
        open a new tab instead of an in-page modal, in which case this is the popup,
        not the original page, and callers must use it for fill/screenshot.

        apply_type: 'native' (Dice easy-apply form), 'external' (redirects to an ATS),
        or 'none' (no apply form found). Tests override this seam with canned data,
        so changes here never affect the mocked test paths.
        """
        detection_log: list[dict] = []
        self._dismiss_known_overlays(page, detection_log)
        if self._has_external_apply(page, detection_log):
            self._trace_return("_detect_apply_form", "external apply control matched -> apply_type=external",
                               detection_log, url=self._safe_page_url(page))
            return {"apply_type": "external", "fields": [], "submit_present": False, "detection_log": detection_log, "page": page}
        active_page = self._open_native_apply(page, detection_log)
        if active_page is None:
            self._trace_return("_detect_apply_form", "no Apply control matched any selector -> apply_type=none",
                               detection_log, url=self._safe_page_url(page),
                               selectors_tried=list(self.NATIVE_APPLY_SELECTORS))
            return {"apply_type": "none", "fields": [], "submit_present": False, "detection_log": detection_log, "page": page}

        # The wizard is a fresh page load: the CMP re-injects its banner there, so
        # clear it again (and wait for it to go) before reading/clicking anything.
        self._handle_cookie_banner(active_page, detection_log)

        # Wait for the Dice Easy Apply wizard (same-tab SPA navigation or popup) to
        # actually load its fields before reading them -- this is what previously
        # failed: the browser closed after ~2s against a half-loaded / detail page.
        panel = self._wait_for_apply_panel(active_page)
        detection_log.append({"step": "wizard_wait", **panel})

        fields = self._gather_form_fields(active_page, detection_log)
        detection_log.append(
            {"step": "fields_detected", "count": len(fields), "labels": [f.get("label") for f in fields][:25]}
        )
        wizard_reached = bool(panel.get("wizard_detected")) or self._looks_like_wizard(active_page)
        # Zero fields is NOT a failure when the wizard walked all the way to the
        # review/submit step: Step 1's fields are deliberately discarded (it is
        # navigation-only) and a review page may carry no inputs of its own.
        if not fields and getattr(self, "_wizard_stop_code", None) == "reached_submit":
            return {"apply_type": "native", "fields": [], "submit_present": True,
                    "detection_log": detection_log, "page": active_page}
        if not fields:
            # Distinguish "wizard opened but fields could not be read" (unsupported,
            # with saved screenshot+HTML for debugging) from "apply never opened".
            apply_type = "wizard_no_fields" if wizard_reached else "none"
            self._capture_named(active_page, "no_fields_detected")
            self._capture_html(active_page, "no_fields_detected")
            self._trace_return("_detect_apply_form", f"no fields collected -> apply_type={apply_type}",
                               detection_log, url=self._safe_page_url(active_page),
                               wizard_reached=wizard_reached, panel=panel)
            return {"apply_type": apply_type, "fields": [], "submit_present": False,
                    "detection_log": detection_log, "page": active_page}
        return {
            "apply_type": "native",
            "fields": fields,
            "submit_present": self._has_submit(active_page),
            "detection_log": detection_log,
            "page": active_page,
        }

    # ---- Cookie consent banner (Dice uses consentmanager.net) ----
    #
    # The captured wizard HTML (outputs/dice_debug/.../job_33072/apply_form.html)
    # loads https://delivery.consentmanager.net/delivery/cmp.php?id=30394 and
    # cmp_final.min.js, and tags nodes with data-cmp-ab. The banner itself is NOT
    # in that capture because the CMP injects it client-side after load -- which is
    # precisely how it can appear late and intercept a Next click. Selectors below
    # are consentmanager's own (cmpbox/cmpwelcomebtnyes), with generic text and
    # OneTrust fallbacks. Regardless of these, _click_next independently detects
    # interception via elementFromPoint, so an unknown overlay is still handled.
    COOKIE_BANNER_SELECTORS = (
        "#cmpwrapper",
        "#cmpbox",
        ".cmpboxbtn",
        "#onetrust-banner-sdk",
    )
    COOKIE_ACCEPT_SELECTORS = (
        "#cmpwelcomebtnyes",          # consentmanager "Allow all"
        "#cmpbox a.cmpboxbtnyes",
        ".cmpboxbtnyes",
        "#onetrust-accept-btn-handler",
        "button:has-text('Allow all')",
        "a:has-text('Allow all')",
        "button:has-text('Accept all')",
    )
    COOKIE_DISMISS_SELECTORS = (
        ".cmpboxbtnclose",
        "#cmpbox .cmpboxbtnclose",
        "button:has-text('Reject all')",
        "[aria-label='Close' i]",
    )
    _cookie_banner_max_polls = 10

    def _cookie_banner_visible(self, page):
        """Return (visible, matched_selector) for a consent banner. The banner's own
        container is checked first; a visible "Allow all" control also counts, since
        consentmanager renders the box into a shadow root whose host id can change.
        Never raises."""
        for selector in self.COOKIE_BANNER_SELECTORS + self.COOKIE_ACCEPT_SELECTORS:
            try:
                control = page.locator(selector).first
                if control.count() > 0 and control.is_visible():
                    return True, selector
            except Exception:
                continue
        return False, None

    def _wait_for_banner_gone(self, page) -> bool:
        """Wait until the consent banner is no longer visible. Never raises."""
        for _ in range(self._cookie_banner_max_polls):
            visible, _ = self._cookie_banner_visible(page)
            if not visible:
                return True
            try:
                page.wait_for_timeout(300)
            except Exception:
                break
        return not self._cookie_banner_visible(page)[0]

    def _handle_cookie_banner(self, page, log: list[dict] | None = None) -> dict:
        """Dice-only: accept ("Allow all") or dismiss the consent banner, then WAIT
        for it to disappear before automation continues. Idempotent and safe to call
        on every Dice page. Never raises; never submits anything."""
        diag = {"detected": False, "dismissed": False, "method": None, "matched": None}
        visible, matched = self._cookie_banner_visible(page)
        if not visible:
            return diag
        diag["detected"] = True
        diag["matched"] = matched
        if log is not None:
            log.append({"step": "cookie_banner_detected", "matched": matched,
                        "url": self._safe_page_url(page)})

        for selector in self.COOKIE_ACCEPT_SELECTORS + self.COOKIE_DISMISS_SELECTORS:
            try:
                control = page.locator(selector).first
                if control.count() == 0 or not control.is_visible():
                    continue
                control.click(timeout=3000)
                diag["method"] = selector
                break
            except Exception:
                continue

        diag["dismissed"] = self._wait_for_banner_gone(page)
        if log is not None:
            log.append({"step": "cookie_banner_dismissed", "cookie_banner_dismissed": diag["dismissed"],
                        "method": diag["method"], "matched": matched})
        return diag

    def _dismiss_known_overlays(self, page, log: list[dict] | None = None) -> None:
        """Best-effort dismissal of cookie/consent banners and interstitial popups
        that can otherwise intercept clicks on the real Apply control. Never raises."""
        for selector in (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept all')",
            "button:has-text('Accept cookies')",
            "button:has-text('Not now')",
            "button:has-text('Skip')",
            "[aria-label='Close' i]",
        ):
            try:
                control = page.locator(selector).first
                if control.count() > 0 and control.is_visible():
                    control.click(timeout=1500)
                    page.wait_for_timeout(200)
                    if log is not None:
                        log.append({"step": "overlay_dismissed", "matched": selector})
            except Exception:
                continue

    def _gather_form_fields(self, page, log: list[dict] | None = None) -> list[dict]:
        """Drive the (possibly multi-step) Dice Easy Apply wizard in DRY RUN.

        Step 1 ("Resume & Cover Letter") is a NAVIGATION-ONLY page: the automation
        touches nothing on it -- no resume upload/replace/verify, no cover letter,
        no filename or resume-id inspection. Its sole job is cookie banner -> Next
        -> confirm the heading advanced.

        Every later step: fill that step's fields, then -- if a Submit/review control
        is present -- STOP before it; otherwise click Next and WAIT for the next
        step to render before continuing. Every stop is classified with an exact
        code (self._wizard_stop_code) so the browser never closes on a false
        success. Returns the accumulated fields. NEVER clicks Submit.
        """
        fields: list[dict] = []
        seen: set[tuple[str, str]] = set()
        trace: list[dict] = []
        stopped = False
        for step in range(1, self._max_wizard_steps + 1):
            self._wizard_last_step = step
            self._wizard_last_url = self._safe_page_url(page)
            self._capture_step(page, step)
            trace.append({"wizard_step": step, "url": self._wizard_last_url, "event": "step_opened"})
            if log is not None:
                log.append({"step": "wizard_step_opened", "wizard_step": step, "url": self._wizard_last_url})

            # Fields are collected only to CLASSIFY the step. On Step 1 they are
            # discarded without inspection: nothing on that page is interacted with.
            collected = self._collect_fields(page)
            step_name = self._detect_step_name(page, collected)
            self._current_step_name = step_name
            is_step1 = step_name == "resume_cover_letter"

            step_fields = []
            if not is_step1:
                for field in collected:
                    key = (str(field.get("label")), str(field.get("control")))
                    if key in seen:
                        continue
                    seen.add(key)
                    fields.append(field)
                    step_fields.append(field)
            trace.append({"wizard_step": step, "event": "fields_collected", "step_name": step_name,
                          "labels": [f.get("label") for f in step_fields][:25]})
            if log is not None:
                log.append({"step": "wizard_step_detected", "wizard_step": step, "step_name": step_name})

            # Full per-step snapshot: everything needed to explain this step without
            # re-running live.
            next_found, next_selector = self._find_next_control(page)
            snapshot = {
                "current_step_number": step,
                "current_step_name": step_name,
                "progress_bar_text": self._progress_text(page),
                "page_heading": self._page_heading_text(page)[:300],
                "page_url": self._wizard_last_url,
                "fields_detected": [
                    {"label": f.get("label"), "kind": f.get("kind"),
                     "control": f.get("control"), "required": f.get("required")}
                    for f in step_fields
                ],
                "field_count": len(step_fields),
                "navigation_only": is_step1,
                "next_button_found": next_found,
                "next_button_selector": next_selector,
                "dom_fingerprint": self._dom_fingerprint(page),
            }
            self._trace("wizard_step_snapshot", **snapshot)
            trace.append({"event": "step_snapshot", **snapshot})
            if log is not None:
                log.append({"step": "wizard_step_snapshot", **snapshot})

            # ---- Step 1: navigation only. Touch nothing; just get to Step 2. ----
            if is_step1:
                if self._navigate_step1(page, step, log, trace):
                    continue
                stopped = True
                break

            if step_name == "additional_information":
                self._trace("step2_started", url=self._wizard_last_url,
                            fields=[f.get("label") for f in step_fields])
                if log is not None:
                    log.append({"step": "step2_started", "wizard_step": step,
                                "fields": [f.get("label") for f in step_fields]})
            elif step_name == "review_submit":
                self._trace("review_page_reached", url=self._wizard_last_url)
                if log is not None:
                    log.append({"step": "review_page_reached", "wizard_step": step,
                                "url": self._wizard_last_url})

            stop = self._fill_current_step(page, step_fields, log)
            if stop is None and step_name == "additional_information":
                self._trace("step2_completed", url=self._safe_page_url(page))
                if log is not None:
                    log.append({"step": "step2_completed", "wizard_step": step})

            if stop == "needs_review":
                self._set_wizard_stop(
                    "missing_required_answer",
                    "A required Dice field could not be answered from saved data.",
                    trace,
                )
                stopped = True
                break
            if stop == "unsupported":
                self._set_wizard_stop("unsupported_form", "The Dice step contained an unsupported control.", trace)
                stopped = True
                break

            # Reached the review/submit step -> STOP before Submit (dry-run success).
            # (Step 1 already returned above, so a `type=submit` Next control there
            # can never be mistaken for the review step.)
            if self._has_submit(page):
                if step_name != "review_submit":
                    self._trace("review_page_reached", url=self._safe_page_url(page))
                    if log is not None:
                        log.append({"step": "review_page_reached", "wizard_step": step,
                                    "url": self._safe_page_url(page)})
                # Dry run: verify every required field is complete BEFORE declaring
                # the application submit-ready. Incomplete -> needs_review, and the
                # browser still closes only after the status is recorded.
                missing = self._incomplete_required_fields(page)
                self._review_missing_required = missing
                if missing:
                    self._set_wizard_stop(
                        "missing_required_answer",
                        f"Dice review step still has incomplete required fields: {', '.join(missing[:6])}.",
                        trace,
                    )
                    if log is not None:
                        log.append({"step": "review_incomplete", "missing_required": missing})
                    stopped = True
                    break
                self._trace("ready_for_submit", url=self._safe_page_url(page), submitted=False)
                if log is not None:
                    log.append({"step": "ready_for_submit", "wizard_step": step, "submitted": False})
                self._set_wizard_stop("reached_submit",
                                      "Reached the Dice review/submit step; application was NOT submitted (dry run).", trace)
                if log is not None:
                    log.append({"step": "stopped_before_submit", "wizard_step": step})
                stopped = True
                break

            # Otherwise advance with Next and confirm the next step renders.
            advanced, why = self._advance_and_wait(page, step_fields, log)
            trace.append({"wizard_step": step, "event": "advance", "advanced": advanced, "why": why})
            if not advanced:
                if why == "next_not_found":
                    # A genuinely single-step form with no further Next/Submit: the
                    # fields we found are filled -> submit-ready.
                    self._set_wizard_stop("reached_end",
                                          "No further Dice steps; all detected fields were filled (dry run).", trace)
                else:
                    self._set_wizard_stop(why, self._advance_reason_text(why), trace)
                    if why == "next_not_found":
                        # Save the exact DOM so the real Next selector can be pinned.
                        self._capture_named(page, "next_not_found")
                        self._capture_html(page, "next_not_found")
                        if log is not None:
                            log.append({"step": "next_not_found", "wizard_step": step,
                                        "step_name": step_name, "url": self._safe_page_url(page),
                                        "selectors_tried": list(self.NEXT_SELECTORS)})
                stopped = True
                break
            if log is not None:
                log.append({"step": "next_clicked", "wizard_step": step})
                if step_name == "additional_information":
                    log.append({"step": "step2_next_clicked", "wizard_step": step})
                log.append({"step": "next_step_detected", "wizard_step": step + 1, "from_step_name": step_name})
            if step_name == "additional_information":
                self._trace("step2_next_clicked", url=self._safe_page_url(page))
        if not stopped:
            self._set_wizard_stop("timeout", "Exceeded the Dice wizard step limit before reaching Submit.", trace)

        self._capture_named(page, "before_close")
        self._capture_html(page, "before_close")
        self._write_wizard_trace(trace)
        return fields

    _step1_max_next_attempts = 3
    _step_change_max_polls = 10

    def _navigate_step1(self, page, step: int, log: list[dict] | None, trace: list[dict]) -> bool:
        """Dice Step 1 ("Resume & Cover Letter") is NAVIGATION ONLY.

        The automation interacts with NOTHING on this page: it does not upload,
        replace, verify or inspect the resume, never reads its filename, never
        compares resume ids, never opens the resume menu, and never touches the
        cover letter. Its only responsibility is reaching Additional Information:

            cookie banner -> Next (visible, enabled, scrolled, unobstructed)
            -> confirm the heading advanced past "Resume & Cover Letter".

        Retries the Next click up to `_step1_max_next_attempts` times. On failure it
        records stop_code=next_not_working with a screenshot, page HTML and the apply
        trace, and returns False (-> needs_review). Never closes without a reason.
        Returns True when the wizard advanced.
        """
        heading = self._page_heading_text(page)[:300]
        self._trace("step1_entered", url=self._safe_page_url(page), heading=heading)
        trace.append({"wizard_step": step, "event": "step1_entered", "heading": heading})
        if log is not None:
            log.append({"step": "step1_entered", "wizard_step": step, "heading": heading,
                        "url": self._safe_page_url(page), "navigation_only": True})

        banner = self._handle_cookie_banner(page, log)
        self._trace("cookie_banner_handled", **banner)
        if log is not None:
            log.append({"step": "cookie_banner_handled", "detected": banner["detected"],
                        "dismissed": banner["dismissed"], "method": banner["method"]})

        last_reason = None
        for attempt in range(1, self._step1_max_next_attempts + 1):
            found, selector = self._find_next_control(page)
            self._trace("next_button_found", found=found, selector=selector, attempt=attempt)
            if log is not None:
                log.append({"step": "next_button_found", "found": found,
                            "selector": selector, "attempt": attempt})

            url_before = self._safe_page_url(page)
            dom_before = self._dom_fingerprint(page)
            clicked, why = self._click_next(page, log)
            if not clicked:
                last_reason = why
                self._clear_click_blockers(page, log)
                continue

            self._trace("next_clicked", url_before=url_before, url_after=self._safe_page_url(page),
                        dom_changed=self._dom_fingerprint(page) != dom_before, attempt=attempt)
            if log is not None:
                log.append({"step": "next_clicked", "wizard_step": step, "attempt": attempt})

            advanced_to = self._wait_for_step_change(page, heading)
            if advanced_to:
                self._trace("step1_completed", advanced_to=advanced_to, attempts=attempt)
                if log is not None:
                    log.append({"step": "step1_completed", "wizard_step": step,
                                "advanced_to": advanced_to, "attempts": attempt})
                    log.append({"step": "step2_entered", "wizard_step": step + 1,
                                "step_name": advanced_to, "url": self._safe_page_url(page)})
                self._trace("step2_entered", step_name=advanced_to, url=self._safe_page_url(page))
                trace.append({"wizard_step": step, "event": "step1_completed", "advanced_to": advanced_to})
                return True

            # Clicked, but the heading never changed -> clear overlays and retry.
            last_reason = "heading_unchanged"
            self._trace("step1_not_advanced", attempt=attempt,
                        heading=self._page_heading_text(page)[:300])
            self._clear_click_blockers(page, log)

        self._set_wizard_stop(
            "next_not_working",
            "Dice Step 1 (Resume & Cover Letter) did not advance to the next step after "
            f"{self._step1_max_next_attempts} Next attempts (last reason: {last_reason}).",
            trace,
        )
        self._capture_named(page, "next_not_working")
        self._capture_html(page, "next_not_working")
        self._write_apply_trace()
        if log is not None:
            log.append({"step": "next_not_working", "wizard_step": step,
                        "attempts": self._step1_max_next_attempts, "last_reason": last_reason,
                        "url": self._safe_page_url(page),
                        "selectors_tried": list(self.NEXT_SELECTORS)})
        return False

    def _wait_for_step_change(self, page, initial_heading: str) -> str | None:
        """Poll until the wizard leaves the Resume & Cover Letter step, i.e. the
        heading changed to Additional Information (or any later step). Returns the
        new step name, or None if the page never advanced.

        An 'unknown' classification is treated as still-loading unless the heading
        itself demonstrably changed -- otherwise a half-rendered page would look
        like a successful advance."""
        for _ in range(self._step_change_max_polls):
            name = self._detect_step_name(page, self._collect_fields(page))
            if name not in {"resume_cover_letter", "unknown"}:
                return name
            if name == "unknown":
                heading = self._page_heading_text(page)
                if heading and heading != initial_heading:
                    return "unknown"
            try:
                page.wait_for_timeout(400)
            except Exception:
                break
        return None

    def _set_wizard_stop(self, code: str, reason: str, trace: list[dict] | None = None) -> None:
        """Record WHY (and at exactly which source line) the wizard stopped. This is
        the single choke point for every non-exception end of the step runner."""
        at = self._caller_loc(depth=2)   # the line in _gather_form_fields that stopped
        self._wizard_stop_code = code
        self._wizard_stop_reason = reason
        self._wizard_stop_at = at
        self._trace("wizard_stop", code=code, reason=reason, stop_at=at,
                    last_step=getattr(self, "_wizard_last_step", None),
                    last_url=getattr(self, "_wizard_last_url", None))
        if trace is not None:
            trace.append({"event": "stop", "code": code, "reason": reason, "at": at})

    def _advance_reason_text(self, code: str) -> str:
        return {
            "next_not_found": "Could not find a Next/Continue control after the current Dice step.",
            "next_click_failed": "Found the Dice Next control but the click did not take effect.",
            "step2_not_detected": "Clicked Next but the following Dice step did not render its fields.",
        }.get(code, "Dice wizard could not advance.")

    def _detect_step_name(self, page, step_fields: list[dict]) -> str:
        """Classify the current Dice wizard step: 'resume_cover_letter',
        'additional_information', 'review_submit' or 'unknown'. Heading text is
        authoritative; the step's fields are the fallback when the heading cannot
        be read (e.g. the SPA renders it late)."""
        heading = self._page_heading_text(page)
        if heading:
            for name, pattern in self.STEP_NAME_PATTERNS:
                if pattern.search(heading):
                    return name
        for field in step_fields:
            label = str(field.get("label") or "")
            if str(field.get("kind") or "") == "file" or re.search(r"resume|cover\s*letter", label, re.I):
                return "resume_cover_letter"
        # Field markers beat the submit probe: Dice submits the form on every step,
        # so a step carrying Work Authorization / City is Additional Information even
        # when its Next control looks submit-like.
        for field in step_fields:
            if re.search(r"work\s*authorization|city\s*of\s*residence", str(field.get("label") or ""), re.I):
                return "additional_information"
        if self._has_submit(page):
            return "review_submit"
        return "unknown"

    def _incomplete_required_fields(self, page) -> list[str]:
        """Labels of required controls that are still empty on the current step.
        Used to verify the review step before recording dry_run_ready."""
        result = self._safe_eval(
            page,
            """() => Array.from(document.querySelectorAll('input, select, textarea'))
                .filter(el => el.required && !['hidden', 'submit', 'button'].includes((el.type || '').toLowerCase()))
                .filter(el => (el.type || '').toLowerCase() === 'file'
                    ? !(el.files && el.files.length)
                    : !String(el.value || '').trim())
                .map(el => String(el.getAttribute('aria-label') || el.name || el.id || 'unnamed field'))
                .slice(0, 20)""",
            list, [],
        )
        return [item for item in result if isinstance(item, str)]

    def _is_cover_letter_field(self, field: dict) -> bool:
        return bool(re.search(r"cover\s*letter", str(field.get("label") or ""), re.I))

    def _find_next_control(self, page):
        """Locate a Next/Continue control WITHOUT clicking it. Returns
        (found, selector_or_None). Used by the per-step snapshot so a missing Next
        is visible in the trace before any click is attempted."""
        for selector in self.NEXT_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() > 0 and self._is_visible(button):
                    return True, selector
            except Exception:
                continue
        try:
            button = page.get_by_role("button", name=self.NEXT_ROLE_NAME).first
            if button.count() > 0 and self._is_visible(button):
                return True, "role=button:Next"
        except Exception:
            pass
        return False, None

    def _page_heading_text(self, page) -> str:
        try:
            value = page.evaluate(
                """() => Array.from(document.querySelectorAll("h1, h2, h3, legend, [class*='heading' i], [class*='header' i]"))
                        .map(el => ((el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim())
                        .filter(Boolean).slice(0, 12).join(' | ')"""
            )
            return value if isinstance(value, str) else ""
        except Exception:
            return ""

    def _advance_and_wait(self, page, before_fields: list[dict], log: list[dict] | None):
        """Click Next (waiting for it to render/enable) then wait for the next step
        to render. Returns (advanced, reason_code). reason_code is one of
        'next_not_found' / 'next_click_failed' / 'step2_not_detected'."""
        url_before = self._safe_page_url(page)
        dom_before = self._dom_fingerprint(page)
        self._trace("advance_started", url_before=url_before, dom_before=dom_before)

        clicked, why = self._click_next(page, log)
        if not clicked:
            self._trace_return("_advance_and_wait", f"Next was not clicked ({why})", log,
                               next_clicked=False, reason_code=why, url_before=url_before,
                               url_after=self._safe_page_url(page),
                               dom_changed=self._dom_fingerprint(page) != dom_before)
            return False, why

        url_after = self._safe_page_url(page)
        dom_after = self._dom_fingerprint(page)
        self._trace("next_clicked", url_before=url_before, url_after=url_after,
                    dom_changed=dom_after != dom_before)

        if self._wait_for_next_step(page, before_fields):
            self._trace("next_step_rendered", url_before=url_before,
                        url_after=self._safe_page_url(page),
                        dom_changed=self._dom_fingerprint(page) != dom_before)
            return True, None

        self._trace_return("_advance_and_wait",
                           "Next was clicked but the following step never rendered new fields or a submit control",
                           log, next_clicked=True, reason_code="step2_not_detected",
                           url_before=url_before, url_after=self._safe_page_url(page),
                           dom_changed=self._dom_fingerprint(page) != dom_before)
        return False, "step2_not_detected"

    _next_click_max_attempts = 3

    def _click_next(self, page, log: list[dict] | None = None):
        """Poll for a Next/Continue control to appear AND become enabled, then click
        it. Returns (clicked, reason_code_or_None)."""
        found_disabled = False
        for _ in range(self._next_button_max_polls):
            for selector in self.NEXT_SELECTORS:
                try:
                    button = page.locator(selector).first
                    if button.count() == 0:
                        continue
                    if not self._is_visible(button):
                        continue
                    if not self._is_enabled(button):
                        found_disabled = True
                        continue
                    return self._click_next_control(page, button, selector, log)
                except Exception:
                    continue
            # Role-based fallback: some Dice wizard builds render Next/Continue
            # without any of the selector hooks above.
            try:
                button = page.get_by_role("button", name=self.NEXT_ROLE_NAME).first
                if button.count() > 0 and self._is_visible(button):
                    if self._is_enabled(button):
                        return self._click_next_control(page, button, "role=button:Next", log)
                    found_disabled = True
            except Exception:
                pass
            try:
                page.wait_for_timeout(400)
            except Exception:
                break
        code = "next_click_failed" if found_disabled else "next_not_found"
        self._trace_return(
            "_click_next",
            "a Next control was found but stayed disabled for the whole poll budget"
            if found_disabled else
            "no Next control matched any selector within the poll budget",
            log, reason_code=code, polls=self._next_button_max_polls,
            selectors_tried=list(self.NEXT_SELECTORS) + ["role=button:Next"],
            url=self._safe_page_url(page),
        )
        return False, code

    def _click_next_control(self, page, button, selector: str, log: list[dict] | None):
        """Click a located, visible, enabled Next control. Before each attempt the
        button is scrolled into view and checked for being covered by another
        element (the consent banner is the known offender). An intercepted click is
        logged with the offending element, evidenced (screenshot + HTML), the
        overlay is dismissed, and the click is retried."""
        for attempt in range(1, self._next_click_max_attempts + 1):
            if log is not None:
                log.append({"step": "next_click_attempt", "attempt": attempt, "selector": selector})
            try:
                button.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass

            blocker = self._click_obstruction(button)
            if blocker:
                self._record_click_interception(page, blocker, attempt, selector, log)
                if attempt < self._next_click_max_attempts:
                    self._clear_click_blockers(page, log)
                    continue
                self._trace_return("_click_next_control",
                                   f"Next stayed covered by {blocker.get('tag')}#{blocker.get('id')} "
                                   f"after {attempt} attempts", log,
                                   reason_code="next_click_failed", element=blocker, selector=selector)
                return False, "next_click_failed"

            try:
                button.click(timeout=4000)
            except Exception as exc:
                # The click itself failed -- it may still be an interception the
                # pre-check missed (late-injected banner). Identify, clear, retry.
                blocker = self._click_obstruction(button) or {"reason": str(exc)}
                self._record_click_interception(page, blocker, attempt, selector, log)
                if attempt < self._next_click_max_attempts:
                    self._clear_click_blockers(page, log)
                    continue
                self._trace_return("_click_next_control",
                                   f"click() raised on every attempt: {exc}", log, include_stack=True,
                                   reason_code="next_click_failed", element=blocker,
                                   selector=selector, error_type=type(exc).__name__)
                return False, "next_click_failed"

            if log is not None:
                log.append({"step": "next_click_success", "attempt": attempt, "selector": selector})
            self._trace("next_click_success", attempt=attempt, selector=selector)
            return True, None
        self._trace_return("_click_next_control", "exhausted click attempts", log,
                           reason_code="next_click_failed", selector=selector)
        return False, "next_click_failed"

    def _click_obstruction(self, button) -> dict | None:
        """Return a description of the element covering the button's click point, or
        None when the button is clickable / the check cannot run (fakes, detached
        nodes). Selector-independent: works for any unknown overlay."""
        try:
            result = button.evaluate(
                """el => {
                    const r = el.getBoundingClientRect();
                    if (!r.width || !r.height) return { blocked: true, tag: '', id: '', cls: '', text: '', reason: 'zero_size' };
                    const x = r.left + r.width / 2, y = r.top + r.height / 2;
                    const top = document.elementFromPoint(x, y);
                    if (!top) return null;
                    if (top === el || el.contains(top) || top.contains(el)) return null;
                    const cls = top.className && top.className.baseVal !== undefined
                        ? top.className.baseVal : String(top.className || '');
                    return {
                        blocked: true,
                        tag: top.tagName.toLowerCase(),
                        id: top.id || '',
                        cls: cls.slice(0, 160),
                        text: ((top.innerText || top.textContent) || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
                    };
                }"""
            )
        except Exception:
            return None
        return result if isinstance(result, dict) and result.get("blocked") else None

    def _record_click_interception(self, page, blocker: dict, attempt: int, selector: str,
                                   log: list[dict] | None) -> None:
        """Evidence for an intercepted Next click: screenshot, page HTML, and the
        identity of the element that swallowed it."""
        name = f"next_click_intercepted_{attempt}"
        self._capture_named(page, name)
        self._capture_html(page, name)
        if log is not None:
            log.append({
                "step": "click_intercepted_by", "attempt": attempt, "selector": selector,
                "element": blocker, "url": self._safe_page_url(page), "evidence": name,
            })

    def _clear_click_blockers(self, page, log: list[dict] | None) -> None:
        """Dismiss whatever is covering the Next button (consent banner first, then
        the generic interstitials) and give the DOM a moment to settle."""
        self._handle_cookie_banner(page, log)
        self._dismiss_known_overlays(page, log)
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass

    def _is_visible(self, locator) -> bool:
        try:
            return bool(locator.is_visible())
        except Exception:
            return True  # if we cannot tell, let the click decide

    def _is_enabled(self, locator) -> bool:
        try:
            return bool(locator.is_enabled())
        except Exception:
            return True  # if we cannot tell, assume enabled and let the click decide

    def _wait_for_next_step(self, page, before_fields: list[dict]) -> bool:
        """Wait until the step after Next actually renders: new fields appear or a
        Submit/review control is present."""
        before_keys = {(str(f.get("label")), str(f.get("control"))) for f in before_fields}
        for _ in range(self._next_step_max_polls):
            current = self._collect_fields(page)
            current_keys = {(str(f.get("label")), str(f.get("control"))) for f in current}
            if current_keys - before_keys:
                return True
            if self._has_submit(page):
                return True
            try:
                page.wait_for_timeout(400)
            except Exception:
                break
        return False

    def _write_wizard_trace(self, trace: list[dict]) -> None:
        debug_dir = getattr(self, "_active_debug_dir", None)
        if debug_dir is None:
            return
        try:
            self._write_debug(
                Path(debug_dir),
                "wizard_trace.json",
                {
                    "portal": self.portal_name,
                    "last_step": self._wizard_last_step,
                    "last_step_name": getattr(self, "_current_step_name", None),
                    "last_url": self._wizard_last_url,
                    "stop_code": self._wizard_stop_code,
                    "stop_reason": self._wizard_stop_reason,
                    "trace": trace,
                },
            )
        except Exception:
            pass

    def _fill_current_step(self, page, step_fields: list[dict], log: list[dict] | None) -> str | None:
        """Plan and perform the current step's field interactions. Returns a stop
        reason ('needs_review' / 'unsupported') if a required field cannot be
        filled, else None. NEVER clicks Submit."""
        resume = getattr(self, "_active_resume", None)
        job = getattr(self, "_active_job", None)
        candidate = getattr(self, "_active_candidate", None)
        ctx = getattr(self, "_active_screening_context", None) or {}
        fillable: list[dict] = []
        for field in step_fields:
            entry = self._field_plan_entry(field, resume, job, candidate, ctx)
            kind = entry.get("kind")
            action = entry.get("action")

            # File uploads are never automated on Dice. Step 1 (Resume & Cover
            # Letter) is navigation-only and never reaches this code; a stray file
            # input on any later step is skipped rather than filled.
            if kind == "file_upload":
                if log is not None:
                    log.append({"step": "file_upload_skipped", "label": entry.get("label"),
                                "reason": "Dice file uploads are not automated."})
                continue

            if action == "needs_review":
                self._trace_return("_fill_current_step",
                                   f"field '{entry.get('label')}' could not be answered: {entry.get('reason')}",
                                   log, stop="needs_review", label=entry.get("label"), kind=kind)
                return "needs_review"
            if action == "unsupported":
                self._trace_return("_fill_current_step",
                                   f"field '{entry.get('label')}' uses an unsupported control ({entry.get('control')})",
                                   log, stop="unsupported", label=entry.get("label"), kind=kind)
                return "unsupported"
            if action == "fill":
                # Step 2 telemetry: what was chosen, and where it came from.
                if kind == "work_authorization":
                    self._trace("work_authorization_selected", value=entry.get("value"),
                                source=entry.get("value_source"), resolved_from=entry.get("resolved_from"),
                                options=entry.get("options"))
                    if log is not None:
                        log.append({"step": "work_authorization_selected", "value": entry.get("value"),
                                    "source": entry.get("value_source"),
                                    "resolved_from": entry.get("resolved_from")})
                elif entry.get("normalized_field") == "current_location":
                    self._trace("city_detected", city=entry.get("value"), source=entry.get("value_source"))
                    if log is not None:
                        log.append({"step": "city_detected", "city": entry.get("value"),
                                    "source": entry.get("value_source")})
                    # The city is an autocomplete: it must be typed and its first
                    # suggestion selected + committed here. Next is never clicked
                    # until that succeeds.
                    if not self._fill_city_autocomplete(page, entry, log):
                        reason = self.CITY_FAILURE_REASONS.get(
                            self._city_commit_failure, "The Dice city field could not be completed.")
                        self._trace_return("_fill_current_step", reason, log, stop="needs_review",
                                           city_failure=self._city_commit_failure)
                        return "needs_review"
                    continue
                fillable.append(entry)
        if fillable:
            self._fill_fields(page, fillable)
            if log is not None:
                log.append({
                    "step": "step_fields_filled",
                    "count": len(fillable),
                    "labels": [e.get("label") for e in fillable][:25],
                })
        return None

    def _capture_step(self, page, step: int) -> None:
        debug_dir = getattr(self, "_active_debug_dir", None)
        if debug_dir is None:
            return
        try:
            page.screenshot(path=str(Path(debug_dir) / f"wizard_step_{step}.png"), full_page=True)
        except Exception:
            pass

    EXTERNAL_APPLY_SELECTORS = (
        "a:has-text('Apply on company site')",
        "a:has-text('Apply on employer site')",
        "[data-cy='external-apply']",
    )

    def _has_external_apply(self, page, log: list[dict] | None = None) -> bool:
        for selector in self.EXTERNAL_APPLY_SELECTORS:
            try:
                if page.locator(selector).first.count() > 0:
                    if log is not None:
                        log.append({"step": "external_apply_detected", "matched": selector})
                    self._trace("external_apply_detected", matched=selector,
                                url=self._safe_page_url(page))
                    return True
            except Exception:
                continue
        self._trace("external_apply_not_detected", selectors_tried=list(self.EXTERNAL_APPLY_SELECTORS))
        return False

    # Bare "Apply" (no "Easy"/"now" suffix) is the real control on live Dice job
    # pages — confirmed from a captured screenshot of a real apply attempt where
    # every prior selector missed it and the flow was wrongly classified as
    # "no native apply form found" for every job.
    NATIVE_APPLY_SELECTORS = (
        "[data-cy='easy-apply-button']",
        "[data-cy='apply-button']",
        "[data-testid='apply-button']",
        "[data-testid='easyApply-btn-title']",
        "button:has-text('Easy apply')",
        "button:has-text('Easy Apply')",
        "button:has-text('Quick Apply')",
        "button:has-text('Apply now')",
    )
    NATIVE_APPLY_ROLE_NAME = re.compile(r"^\s*(easy\s*apply|quick\s*apply|apply\s*now|apply)\s*$", re.I)

    # The Dice Easy Apply wizard is a SAME-TAB SPA navigation to /job-applications/
    # <id>/start-apply -> /wizard, NOT an in-page modal. Reaching this URL is how we
    # know the native apply flow actually opened (confirmed from captured live HTML
    # where the CTA is <a data-testid="apply-button" href="/job-applications/.../start-apply">).
    APPLY_FLOW_URL_RE = re.compile(r"/job-applications/", re.I)
    # The apply CTA is often an <a> whose href points straight at the apply flow;
    # navigating to it directly is more reliable than clicking a late-rendered button.
    APPLY_HREF_RE = re.compile(r"/job-applications/|/apply", re.I)
    # Selectors that specifically mark the apply control on a job-detail page; we
    # wait for one of these to (client-side) render before searching.
    APPLY_CONTROL_SELECTORS = (
        "[data-testid='apply-button']",
        "[data-cy='apply-button']",
        "[data-cy='easy-apply-button']",
    )
    # Poll budget for the wizard to load its fields (~20s at 500ms/poll). Overridable
    # in tests to keep them fast.
    _apply_panel_max_polls = 40

    # Safety cap on how many wizard steps we advance through in dry run.
    _max_wizard_steps = 6
    # Controls that advance to the next wizard step (never a Submit control).
    #
    # The first entry is the ACTUAL live Dice Step 1 control, taken verbatim from a
    # captured wizard page (outputs/dice_debug/.../job_33072/apply_form.html):
    #   <button class="... bg-interaction ..." data-rac="" type="submit" tabindex="0"
    #           id="react-aria-_R_5d5..."><span class="...">Next</span></button>
    # It carries no data-cy/data-testid hook and its label lives in a nested <span>,
    # so it is matched by type + text.
    NEXT_SELECTORS = (
        "button[type='submit']:has-text('Next')",
        "[data-cy='next-step']",
        "[data-testid*='next' i]",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Save and continue')",
        "button:has-text('Save & continue')",
    )
    # Role-based fallback for the Next control (some Dice wizard builds render it
    # without any of the selector hooks above).
    NEXT_ROLE_NAME = re.compile(r"^\s*(next|continue|save\s*(and|&)\s*continue)\s*$", re.I)
    _next_button_max_polls = 8   # wait for Next to render/enable after upload
    _next_step_max_polls = 8     # wait for the following step to render after Next

    # Explicit Dice wizard step names, detected from the step heading first and
    # from the step's fields as a fallback. Step 1 (resume_cover_letter) is pure
    # NAVIGATION: it must end in Next, never in a dry_run_ready classification.
    STEP_NAME_PATTERNS = (
        ("resume_cover_letter", re.compile(r"resume\s*(&|and)\s*cover\s*letter", re.I)),
        ("additional_information", re.compile(r"additional\s*information", re.I)),
        ("review_submit", re.compile(r"review\s*(your\s*)?application|review\s*(&|and)\s*submit", re.I)),
    )

    def _open_native_apply(self, page, log: list[dict] | None = None):
        """Find and activate the Apply control. Returns the active page fields
        should be collected from -- the original page after a same-tab wizard
        navigation, or a popup if Dice opened the application in a new tab -- or
        None if nothing matched."""
        # The apply CTA is client-side rendered and can appear late; wait for it.
        self._wait_for_apply_control(page)
        tried: list[str] = []
        for selector in self.NATIVE_APPLY_SELECTORS:
            tried.append(selector)
            try:
                control = page.locator(selector).first
                if control.count() > 0 and self._is_actionable(control):
                    active_page = self._activate_apply_control(page, control)
                    if log is not None:
                        log.append({
                            "step": "apply_button_search", "matched": selector, "found": True,
                            "opened_new_tab": active_page is not page,
                            "wizard_url": self._safe_page_url(active_page),
                        })
                    return active_page
            except Exception:
                continue
        # Fall back to a role-based match for a plain "Apply" button or link,
        # since the real control frequently has no distinguishing selector.
        for role in ("button", "link"):
            tried.append(f"role={role}:Apply")
            try:
                control = page.get_by_role(role, name=self.NATIVE_APPLY_ROLE_NAME).first
                if control.count() > 0 and self._is_actionable(control):
                    active_page = self._activate_apply_control(page, control)
                    if log is not None:
                        log.append({
                            "step": "apply_button_search", "matched": f"role={role}", "found": True,
                            "opened_new_tab": active_page is not page,
                            "wizard_url": self._safe_page_url(active_page),
                        })
                    return active_page
            except Exception:
                continue
        if log is not None:
            log.append({"step": "apply_button_search", "tried": tried, "found": False})
        return None

    def _wait_for_apply_control(self, page) -> None:
        """Wait for the (client-side-rendered) apply button to appear before we
        search for it -- otherwise half the jobs 'find no apply button'. Never raises."""
        try:
            page.wait_for_selector(", ".join(self.APPLY_CONTROL_SELECTORS), timeout=8000, state="visible")
        except Exception:
            pass

    def _activate_apply_control(self, page, locator):
        """Open the Dice apply flow. Prefer navigating straight to the control's
        apply href (deterministic; avoids the click / late-render race); fall back
        to clicking (which may trigger a same-tab wizard nav or a popup)."""
        href = None
        try:
            href = locator.get_attribute("href", timeout=1500)
        except Exception:
            href = None
        if href and self.APPLY_HREF_RE.search(href):
            target = self._absolute_dice_url(href)
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=30000)
                return page
            except Exception:
                pass  # fall through to a click
        return self._click_apply_control(page, locator)

    def _looks_like_wizard(self, page) -> bool:
        return bool(self.APPLY_FLOW_URL_RE.search(self._safe_page_url(page)))

    def _count_visible_buttons(self, page) -> int:
        try:
            return int(
                page.evaluate(
                    """() => Array.from(document.querySelectorAll('button, a[role=button], [type=submit]'))
                        .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }).length"""
                )
                or 0
            )
        except Exception:
            return 0

    def _is_actionable(self, locator) -> bool:
        try:
            return bool(locator.is_visible()) and bool(locator.is_enabled())
        except Exception:
            return False

    def _click_apply_control(self, page, locator):
        """Click the Apply control. If it opens a new tab (Dice sometimes routes
        the application through a popup rather than an in-page modal), return
        that popup as the new active page; otherwise return the original page.

        Uses a "page" event listener rather than `expect_page()` deliberately:
        `expect_page(timeout=...)` and `locator.click(timeout=...)` both raise
        the same PlaywrightTimeoutError, which would make a genuine click
        failure indistinguishable from "no popup opened" -- and a real click
        failure must still propagate so the caller tries the next selector.
        """
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        context = getattr(page, "context", None)
        popups: list = []
        listener = popups.append
        listener_registered = False
        if context is not None:
            try:
                context.on("page", listener)
                listener_registered = True
            except Exception:
                listener_registered = False
        try:
            locator.click(timeout=4000)  # let a genuine click failure raise
        finally:
            if listener_registered:
                try:
                    context.remove_listener("page", listener)
                except Exception:
                    pass
        if listener_registered and not popups:
            try:
                page.wait_for_timeout(300)  # brief grace period for a delayed popup event
            except Exception:
                pass
        if popups:
            popup = popups[0]
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            return popup
        # No popup -> the click may have triggered a SAME-TAB navigation into the
        # Dice apply wizard (/job-applications/...). Wait for that URL so we don't
        # read the (still job-detail) page before it navigates.
        try:
            page.wait_for_url(self.APPLY_FLOW_URL_RE, timeout=8000)
        except Exception:
            pass
        return page

    def _wait_for_apply_panel(self, page) -> dict:
        """Wait for the Dice Easy Apply wizard/modal to load and render fields
        before we read them. Returns diagnostics for logging. Never raises.

        Robust across: same-tab navigation to /job-applications/.../wizard, a
        popup, and slow client-side rendering (polls for several seconds).
        """
        diag: dict = {
            "url_reached": None, "wizard_detected": False,
            "wizard_loaded": False, "fields_found": 0, "buttons_found": 0,
        }
        # 1) Wait for the apply-flow URL (start-apply / wizard) if navigating there.
        try:
            page.wait_for_url(self.APPLY_FLOW_URL_RE, timeout=8000)
        except Exception:
            pass
        # 2) Let the SPA settle where supported.
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        # 3) Poll for fields until the wizard renders them (or the budget runs out).
        for _ in range(self._apply_panel_max_polls):
            diag["url_reached"] = self._safe_page_url(page)
            diag["wizard_detected"] = self._looks_like_wizard(page)
            fields = self._collect_fields(page)
            if fields:
                diag["fields_found"] = len(fields)
                diag["wizard_loaded"] = True
                break
            try:
                page.wait_for_timeout(500)
            except Exception:
                break
        if not diag["fields_found"]:
            diag["fields_found"] = len(self._collect_fields(page))
            diag["wizard_loaded"] = bool(diag["fields_found"]) or diag["wizard_detected"]
        diag["buttons_found"] = self._count_visible_buttons(page)
        return diag

    def _collect_fields(self, page) -> list[dict]:
        try:
            return page.evaluate(self._collect_fields_script()) or []
        except Exception:
            return []

    # The FINAL submit control, identified by its text/data hooks only.
    #
    # `button[type='submit']` is deliberately NOT here: the live Dice wizard renders
    # its Step 1 **Next** control as
    #   <button type="submit" ...><span>Next</span></button>
    # (captured in outputs/dice_debug/.../job_33072/apply_form.html), so matching on
    # the type attribute made every Step 1 look like the review/submit step. The
    # runner then stopped with 'reached_submit' and closed the browser before ever
    # clicking Next.
    SUBMIT_SELECTORS = (
        "[data-cy='submit-application']",
        "[data-testid='submit-application']",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
    )

    def _has_submit(self, page) -> bool:
        for selector in self.SUBMIT_SELECTORS:
            try:
                if page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _fill_fields(self, page, plan: list[dict]) -> None:
        """Fill planned VALUES in the live form (dry-run). NEVER submits.

        File uploads are never automated on Dice; here we only apply text/select/
        radio values via the JS filler, which dispatches input/change events so
        controlled React inputs pick the value up.
        """
        fillable = [
            entry for entry in plan
            if entry.get("value") is not None and entry.get("action") == "fill"
            and not entry.get("autocomplete")   # city is committed via its listbox
        ]
        if not fillable:
            return
        try:
            page.evaluate(self._fill_fields_script(), fillable)
        except Exception:
            pass

    # Two-word city names ("New York, NY") are allowed, but the optional second word
    # must be separated by a SPACE/TAB -- never \s, which matches a newline and made
    # a real resume yield "Analyst\nPittsburgh, PA" instead of "Pittsburgh, PA".
    _CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z.\-]+(?:[ \t]+[A-Z][a-zA-Z.\-]+)?,[ \t]*[A-Z]{2})\b")

    def _resume_city(self, resume: ResumeProfile | None) -> str | None:
        """Best-effort 'City, ST' extraction from already-parsed resume data /
        raw text. Does NOT modify or re-run the resume parser."""
        if resume is None:
            return None
        for exp in (getattr(resume, "experience", None) or []):
            loc = exp.get("location") if isinstance(exp, dict) else None
            if loc:
                match = self._CITY_STATE_RE.search(str(loc))
                if match:
                    return self._clean_city(match.group(1))
        match = self._CITY_STATE_RE.search(getattr(resume, "raw_text", "") or "")
        return self._clean_city(match.group(1)) if match else None

    def _clean_city(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def _capture_named(self, page, name: str) -> None:
        debug_dir = getattr(self, "_active_debug_dir", None)
        if debug_dir is None:
            return
        try:
            page.screenshot(path=str(Path(debug_dir) / f"{name}.png"), full_page=True)
        except Exception:
            pass

    def _capture_html(self, page, name: str) -> None:
        debug_dir = getattr(self, "_active_debug_dir", None)
        if debug_dir is None:
            return
        try:
            (Path(debug_dir) / f"{name}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass

    def _capture_apply_artifact(self, page, debug_dir: Path) -> None:
        try:
            page.screenshot(path=str(debug_dir / "apply_form.png"), full_page=True)
        except Exception:
            pass
        try:
            (debug_dir / "apply_form.html").write_text(page.content(), encoding="utf-8")
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
        dry_run: bool = True,
    ) -> AutomationResult:
        # dry_run defaults to True so any caller/test that predates live apply stays SAFE:
        # an omitted flag can never mean "submit".
        apply_type = form.get("apply_type")
        # Honor the wizard runner's terminal classification: a run that could not
        # advance past a step is NOT a success -- map its exact reason to a status
        # so the browser never closes on a false dry_run_ready.
        stop_code = getattr(self, "_wizard_stop_code", None)
        stop_reason = getattr(self, "_wizard_stop_reason", None)
        stop_at = getattr(self, "_wizard_stop_at", None)

        # Step 1 (navigation-only) could not be advanced -> needs_review. Checked
        # BEFORE apply_type, because Step 1's fields are deliberately discarded and
        # would otherwise look like "wizard opened but no fields".
        if stop_code == "next_not_working":
            log.append({"step": "wizard_stop", "code": stop_code, "reason": stop_reason,
                        "at": stop_at, "last_url": getattr(self, "_wizard_last_url", None)})
            self._trace_return("_plan_dry_run", f"wizard stop_code={stop_code}: {stop_reason}", log,
                               status="needs_review", stop_code=stop_code, stop_at=stop_at)
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW, stop_reason or "Dice Step 1 did not advance.",
                "dice_apply_needs_review", debug_dir, job, log=log,
            )

        # The city autocomplete never committed -> needs_review. Checked before the
        # field re-planning below, which would otherwise see a perfectly fillable
        # city value and wrongly report dry_run_ready.
        city_failure = getattr(self, "_city_commit_failure", None)
        if city_failure:
            reason = self.CITY_FAILURE_REASONS.get(city_failure, "The Dice city field could not be completed.")
            log.append({"step": "needs_review", "reason": reason, "city_failure": city_failure})
            self._trace_return("_plan_dry_run", f"city_failure={city_failure}: {reason}", log,
                               status="needs_review", city_failure=city_failure)
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW, reason, "dice_apply_needs_review", debug_dir, job, log=log,
            )

        if apply_type in {"external", "none", "wizard_no_fields"}:
            if apply_type == "external":
                reason = "Dice apply redirects to an external ATS; not automated."
            elif apply_type == "wizard_no_fields":
                reason = (
                    "Dice Easy Apply wizard opened but no fillable fields were detected "
                    "before the safety timeout (screenshot and page HTML were saved)."
                )
            else:
                reason = "No Dice native apply form was found for this job."
            log.append({"step": "apply_unsupported", "apply_type": apply_type, "reason": reason})
            self._trace_return("_plan_dry_run", f"apply_type={apply_type}: {reason}", log,
                               status="unsupported_form", apply_type=apply_type)
            return self._dice_result(ApplicationStatus.UNSUPPORTED_FORM, reason, "dice_apply_unsupported_form", debug_dir, job, log=log)

        if stop_code in {"next_not_found", "step2_not_detected", "unsupported_form"}:
            log.append({"step": "wizard_stop", "code": stop_code, "reason": stop_reason,
                        "at": stop_at, "last_url": getattr(self, "_wizard_last_url", None)})
            self._trace_return("_plan_dry_run", f"wizard stop_code={stop_code}: {stop_reason}", log,
                               status="unsupported_form", stop_code=stop_code, stop_at=stop_at)
            return self._dice_result(
                ApplicationStatus.UNSUPPORTED_FORM, stop_reason or "Dice wizard could not be completed.",
                "dice_apply_unsupported_form", debug_dir, job, log=log,
            )
        if stop_code in {"next_click_failed", "timeout", "exception"}:
            log.append({"step": "wizard_stop", "code": stop_code, "reason": stop_reason,
                        "at": stop_at, "last_url": getattr(self, "_wizard_last_url", None)})
            self._trace_return("_plan_dry_run", f"wizard stop_code={stop_code}: {stop_reason}", log,
                               status="failed", stop_code=stop_code, stop_at=stop_at)
            return self._dice_result(
                ApplicationStatus.FAILED, stop_reason or "Dice wizard failed to complete.",
                "dice_apply_failed", debug_dir, job, log=log,
            )
        # 'missing_required_answer', 'reached_submit', 'reached_end', None fall
        # through to the field-based decision below.

        fields = form.get("fields") or []
        plan: list[dict] = []
        screening_answers: list[dict] = []
        missing_contact: list[str] = []
        unanswered: list[dict] = []
        unsupported_controls: list[dict] = []

        for field in fields:
            entry = self._field_plan_entry(field, resume, job, candidate_profile, screening_context)
            plan.append(entry)
            event = entry.get("event") or {}
            log.append(
                {
                    "step": "field_planned",
                    "label": entry.get("label"),
                    "control": entry.get("control"),
                    "kind": entry.get("kind"),
                    "action": entry.get("action"),
                    "answer": entry.get("value"),
                    "source": event.get("source"),
                    "confidence": event.get("confidence"),
                    "category": event.get("category"),
                    "reason": event.get("reason") or entry.get("reason"),
                }
            )
            if entry["kind"] in {"screening", "work_authorization"} and entry.get("event"):
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
            self._trace_return("_plan_dry_run", f"{len(unanswered)} required field(s) unanswered", log,
                               status="needs_review", unanswered=unanswered)
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW, reason, "dice_apply_needs_review", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )
        if unsupported_controls:
            reason = "Dice form contains controls this automation does not yet support."
            log.append({"step": "unsupported_controls", "controls": unsupported_controls})
            self._trace_return("_plan_dry_run", f"{len(unsupported_controls)} unsupported control(s)", log,
                               status="unsupported_form", controls=unsupported_controls)
            return self._dice_result(
                ApplicationStatus.UNSUPPORTED_FORM, reason, "dice_apply_unsupported_form", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )

        # The review step reported required fields still empty -> never dry_run_ready.
        review_missing = getattr(self, "_review_missing_required", None)
        if review_missing:
            reason = ("Dice review step still has incomplete required fields: "
                      f"{', '.join(review_missing[:6])}.")
            log.append({"step": "needs_review", "missing_required": review_missing})
            self._trace_return("_plan_dry_run", reason, log, status="needs_review",
                               missing_required=review_missing)
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW, reason, "dice_apply_needs_review", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )

        # Every safety check above has passed: the form is native, the wizard reached the end,
        # nothing is unanswered, nothing is unsupported, and the review step reported no missing
        # required fields. Only now may the two modes diverge.
        self._fill_fields(page, plan)

        if dry_run:
            # DRY RUN: fill everything EXCEPT final submit. Unchanged behaviour.
            log.append({"step": "dry_run_filled", "fields_planned": len(plan), "submitted": False})
            reason = "Dice dry run reached the submit-ready stage; application was NOT submitted."
            self._trace_return("_plan_dry_run",
                               f"all fields answerable; wizard stop_code={stop_code or 'none'} -> submit-ready", log,
                               status="dry_run_ready", stop_code=stop_code, stop_at=stop_at,
                               fields_planned=len(plan))
            return self._dice_result(
                ApplicationStatus.DRY_RUN_READY, reason, "dice_dry_run_ready", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )

        # LIVE APPLY: really submit. Reaching submit-ready is NOT success here -- only a
        # positively confirmed submission is.
        log.append({"step": "live_apply_filled", "fields_planned": len(plan)})
        return self._submit_application(
            page, job, debug_dir, log,
            screening_answers=screening_answers, missing_contact=missing_contact,
            fields_planned=len(plan),
        )

    # ---- Live submission ------------------------------------------------------------------
    #: Positive proof Dice accepted the application. Absence of proof is NEVER treated as
    #: success -- an unconfirmed click is reported for review, not as APPLIED.
    SUBMIT_CONFIRMATION_RE = re.compile(
        r"application\s+(?:was\s+)?(?:submitted|sent|received|complete)"
        r"|(?:you(?:'ve| have)?\s+)?(?:successfully\s+)?applied\b"
        r"|thanks?\s+for\s+applying"
        r"|your\s+application\s+(?:has\s+been\s+)?(?:submitted|sent)",
        re.I,
    )

    def _find_submit_control(self, page):
        for selector in self.SUBMIT_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_enabled():
                    return locator
            except Exception:
                continue
        return None

    def _submission_confirmed(self, page, timeout_ms: int = 15000) -> bool:
        """Wait for Dice to positively confirm the submission. Returns False on timeout -- the
        caller must NOT report APPLIED without this."""
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            try:
                if self.SUBMIT_CONFIRMATION_RE.search(page.inner_text("body")):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def _submit_application(
        self,
        page,
        job: JobOpportunity,
        debug_dir: Path,
        log: list[dict],
        *,
        screening_answers: list[dict],
        missing_contact: list[str],
        fields_planned: int,
    ) -> AutomationResult:
        """Click Dice's final Submit and confirm it landed.

        APPLIED is returned ONLY on positive confirmation. A click we cannot confirm is
        genuinely ambiguous (it may or may not have gone through), so it is surfaced for review
        rather than recorded as a success or as a clean failure.
        """
        submit = self._find_submit_control(page)
        if submit is None:
            reason = "Dice live apply reached the submit stage but no enabled Submit control was found."
            log.append({"step": "submit_not_found", "submitted": False})
            self._trace_return("_submit_application", reason, log, status="needs_review")
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW, reason, "dice_submit_not_found", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )

        self._capture_named(page, "before_submit")
        try:
            submit.click()
        except Exception as exc:
            reason = f"Dice Submit click failed: {exc}"
            log.append({"step": "submit_click_failed", "submitted": False, "error": str(exc)})
            self._trace_return("_submit_application", reason, log, status="failed")
            return self._dice_result(
                ApplicationStatus.FAILED, reason, "dice_submit_failed", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            )

        self._trace("submit_clicked", job_id=job.id, url=self._safe_page_url(page))
        confirmed = self._submission_confirmed(page)
        self._capture_named(page, "after_submit")
        self._capture_html(page, "after_submit")

        if not confirmed:
            reason = ("Dice Submit was clicked but the confirmation could not be verified. "
                      "Check this application manually before re-applying.")
            log.append({"step": "submit_unconfirmed", "submitted": None})
            self._trace_return("_submit_application", reason, log, status="needs_review")
            return self._dice_result(
                ApplicationStatus.NEEDS_REVIEW, reason, "dice_submit_unconfirmed", debug_dir, job,
                log=log, screening_answers=screening_answers, missing_contact=missing_contact,
                submitted=None,
            )

        reason = "Dice live apply: application submitted and confirmed."
        log.append({"step": "submitted", "fields_planned": fields_planned, "submitted": True})
        self._trace_return("_submit_application", reason, log, status="applied")
        return self._dice_result(
            ApplicationStatus.APPLIED, reason, "dice_submitted", debug_dir, job,
            log=log, screening_answers=screening_answers, missing_contact=missing_contact,
            submitted=True,
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

        # 1) File uploads are NEVER automated on Dice. Resume and cover letter both
        #    live on Step 1, which is navigation-only, so this only guards a stray
        #    file input on a later step.
        is_file = kind == "file" or detection.detected_control_type == "file_upload" or re.search(r"resume|cv|upload", label, re.I)
        if is_file:
            return {**base, "kind": "file_upload", "action": "skip", "value": None,
                    "reason": "Dice file uploads are not automated."}

        # 2) Step 2's Work Authorization dropdown. Resolution order, no invention:
        #    saved screening answer / knowledge base -> configured default. The chosen
        #    value must then MATCH one of the dropdown's real options.
        if self.WORK_AUTH_LABEL_RE.search(label):
            return self._work_authorization_entry(
                base, label, options, required, resume, job, candidate_profile, screening_context
            )

        # 3) Candidate contact identity fields (from saved profile; City falls back
        #    to the parsed resume so a required City field is not needlessly blocked).
        contact_attr = self._contact_attr_for_label(label)
        if contact_attr:
            value = getattr(candidate_profile, contact_attr, None) if candidate_profile else None
            value = (value or "").strip() or None
            source = "candidate_profile"
            if value is None and contact_attr == "current_location":
                value = self._resume_city(resume)
                if value:
                    source = "resume"
            return {**base, "kind": "contact", "action": "fill" if value else ("needs_review" if required else "skip"),
                    "value": value, "normalized_field": contact_attr, "value_source": source,
                    # The city is an autocomplete handled by _fill_city_autocomplete;
                    # the JS filler must never overwrite its committed value.
                    "autocomplete": contact_attr == "current_location",
                    "reason": None if value else f"Candidate profile is missing '{contact_attr}'."}

        # 4) Screening questions -> shared ScreeningAnswerEngine
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

    WORK_AUTH_LABEL_RE = re.compile(
        r"work\s*authorization|authorization\s*status|authorized\s*to\s*work|work\s*status", re.I
    )

    # ---- Step 2 "Current city of residence" is an AUTOCOMPLETE ----
    # Typing "Pittsburgh, PA" is not enough: Dice only commits the value when one of
    # its suggestions ("Pittsburgh, PA, USA") is selected. Type -> wait for the
    # listbox -> click the first option -> verify the input committed -> only then Next.
    CITY_INPUT_SELECTORS = (
        "input[aria-label*='city' i]",
        "input[name*='city' i]",
        "input[id*='city' i]",
        "input[placeholder*='city' i]",
        "input[aria-label*='location' i]",
        "input[name*='location' i]",
    )
    CITY_SUGGESTION_SELECTORS = (
        "[role='listbox'] [role='option']",
        "ul[role='listbox'] li",
        "[role='option']",
        "[class*='autocomplete' i] [role='option']",
        "[class*='autocomplete' i] li",
        "[class*='suggestion' i] li",
    )
    _city_suggestion_max_polls = 10

    def _find_city_input(self, page):
        for selector in self.CITY_INPUT_SELECTORS:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and self._is_visible(loc):
                    return loc, selector
            except Exception:
                continue
        return None, None

    def _type_city(self, loc, city: str) -> None:
        """Enter the city with real keystrokes where possible: some autocompletes
        never open their listbox for a programmatic value set."""
        try:
            loc.click(timeout=3000)
        except Exception:
            pass
        try:
            loc.fill("")
        except Exception:
            pass
        try:
            loc.press_sequentially(city, delay=40, timeout=5000)
            return
        except Exception:
            pass
        try:
            loc.type(city, delay=40)
            return
        except Exception:
            pass
        loc.fill(city)

    def _wait_for_city_suggestions(self, page):
        """Poll for the autocomplete listbox. Returns (options_locator, selector, count)."""
        for _ in range(self._city_suggestion_max_polls):
            for selector in self.CITY_SUGGESTION_SELECTORS:
                try:
                    options = page.locator(selector)
                    count = options.count()
                    if count > 0:
                        return options, selector, count
                except Exception:
                    continue
            try:
                page.wait_for_timeout(300)
            except Exception:
                break
        return None, None, 0

    def _verify_city_committed(self, loc, city: str, suggestion_text: str) -> bool:
        """The input must hold a real value after the suggestion is chosen."""
        try:
            value = str(loc.input_value() or "").strip()
        except Exception:
            # Cannot read the value back; the suggestion click is our best evidence.
            return True
        if not value:
            return False
        lowered = value.casefold()
        head = city.split(",")[0].strip().casefold()
        return bool(head and head in lowered) or lowered == str(suggestion_text or "").strip().casefold()

    def _fill_city_autocomplete(self, page, entry: dict, log: list[dict] | None) -> bool:
        """Type the city, select the FIRST autocomplete suggestion, and verify the
        value committed. Returns False (with self._city_commit_failure set) when the
        city cannot be committed -- the caller must then stop with needs_review and
        must NOT click Next."""
        city = entry.get("value")
        if not city:
            self._city_commit_failure = "city_missing"
            return False

        loc, selector = self._find_city_input(page)
        if loc is None:
            self._city_commit_failure = "city_input_not_found"
            self._capture_named(page, "city_input_not_found")
            self._capture_html(page, "city_input_not_found")
            self._dump_city_autocomplete_dom(page, str(city), None)
            self._trace("city_input_not_found", selectors_tried=list(self.CITY_INPUT_SELECTORS))
            if log is not None:
                log.append({"step": "city_input_not_found",
                            "selectors_tried": list(self.CITY_INPUT_SELECTORS),
                            "dom_dump": "city_autocomplete_debug.json"})
            return False

        self._type_city(loc, str(city))
        self._trace("city_typed", city=city, selector=selector)
        if log is not None:
            log.append({"step": "city_typed", "city": city, "selector": selector})

        options, option_selector, count = self._wait_for_city_suggestions(page)
        if not count:
            self._city_commit_failure = "city_suggestion_not_found"
            # One failed live run must be enough to pin the real Dice selector:
            # screenshot + full page HTML + the DOM around the city input + every
            # visible element mentioning the city, PA, option/listbox/combobox/autocomplete.
            self._capture_named(page, "city_suggestion_not_found")
            self._capture_html(page, "city_suggestion_not_found")
            dump = self._dump_city_autocomplete_dom(page, str(city), selector)
            self._trace("city_suggestion_not_found", city=city,
                        selectors_tried=list(self.CITY_SUGGESTION_SELECTORS),
                        dom_matches=len(dump.get("matches") or []))
            if log is not None:
                log.append({"step": "city_suggestion_not_found", "city": city,
                            "selectors_tried": list(self.CITY_SUGGESTION_SELECTORS),
                            "dom_dump": "city_autocomplete_debug.json",
                            "dom_matches": len(dump.get("matches") or [])})
            return False

        first = options.first
        first_text = self._safe_text(first).strip()
        self._trace("city_suggestions_found", count=count, selector=option_selector,
                    first_suggestion=first_text)
        if log is not None:
            log.append({"step": "city_suggestions_found", "count": count,
                        "selector": option_selector})
            log.append({"step": "city_first_suggestion_text", "text": first_text})

        try:
            first.click(timeout=4000)
        except Exception as exc:
            self._city_commit_failure = "city_suggestion_click_failed"
            self._capture_named(page, "city_suggestion_click_failed")
            if log is not None:
                log.append({"step": "city_suggestion_click_failed", "error": str(exc)})
            return False
        self._trace("city_suggestion_selected", text=first_text)
        if log is not None:
            log.append({"step": "city_suggestion_selected", "text": first_text})

        if not self._verify_city_committed(loc, str(city), first_text):
            self._city_commit_failure = "city_not_committed"
            self._capture_named(page, "city_not_committed")
            if log is not None:
                log.append({"step": "city_not_committed", "city": city, "suggestion": first_text})
            return False

        self._trace("city_committed", city=city, committed_as=first_text)
        if log is not None:
            log.append({"step": "city_committed", "city": city, "committed_as": first_text})
            # Backward-compatible alias for the pre-autocomplete log name.
            log.append({"step": "city_filled", "city": city, "label": entry.get("label")})
        return True

    # Tokens searched for in the failure dump, on top of the typed city and its state.
    CITY_DOM_TOKENS = ("option", "listbox", "combobox", "autocomplete")

    def _dump_city_autocomplete_dom(self, page, city: str, input_selector: str | None) -> dict:
        """Capture everything needed to pin Dice's real city-autocomplete selector
        from ONE failed run: the input's own markup and ARIA wiring, the DOM around
        it, and every visible element mentioning the typed city, its state code, or
        option/listbox/combobox/autocomplete. Never raises."""
        parts = [segment.strip() for segment in str(city).split(",") if segment.strip()]
        tokens = [t for t in ([parts[0]] if parts else []) + ([parts[1]] if len(parts) > 1 else [])]
        tokens += list(self.CITY_DOM_TOKENS)

        script = """
            (() => {
                const TOKENS = __TOKENS__;
                const INPUT_SELECTORS = __INPUT_SELECTORS__;
                const clip = (s, n) => String(s || '').replace(/\\s+/g, ' ').trim().slice(0, n);
                const classOf = el => (el.className && el.className.baseVal !== undefined)
                    ? el.className.baseVal : String(el.className || '');
                const attrsOf = el => {
                    const out = {};
                    for (const a of Array.from(el.attributes || [])) out[a.name] = clip(a.value, 160);
                    return out;
                };
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                    // Hidden-but-present listboxes still matter for selector discovery.
                    return (el.getAttribute('role') || '').match(/option|listbox|combobox/i) !== null;
                };
                const describe = el => ({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    cls: clip(classOf(el), 160),
                    role: el.getAttribute('role') || '',
                    text: clip(el.innerText || el.textContent, 120),
                    attrs: attrsOf(el),
                    visible: visible(el),
                    outer_html: clip(el.outerHTML, 400),
                });

                // 1) The city input itself, plus its ARIA wiring.
                let input = null, matchedSelector = null;
                for (const sel of INPUT_SELECTORS) {
                    const el = document.querySelector(sel);
                    if (el) { input = el; matchedSelector = sel; break; }
                }
                if (!input) {
                    const cands = Array.from(document.querySelectorAll('input, [role=combobox]'));
                    input = cands.find(el => /city|location/i.test(
                        (el.getAttribute('aria-label') || '') + (el.name || '') + (el.id || '') + (el.placeholder || '')
                    )) || null;
                    if (input) matchedSelector = 'heuristic:city|location';
                }

                const aria = {};
                let region_html = '';
                let siblings = [];
                if (input) {
                    for (const a of ['role', 'aria-controls', 'aria-owns', 'aria-expanded',
                                     'aria-activedescendant', 'aria-autocomplete', 'aria-haspopup',
                                     'list', 'autocomplete']) {
                        const v = input.getAttribute(a);
                        if (v) aria[a] = v;
                    }
                    for (const key of ['aria-controls', 'aria-owns', 'aria-activedescendant', 'list']) {
                        const id = input.getAttribute(key);
                        if (id) {
                            const target = document.getElementById(id);
                            aria[key + '_target'] = target ? describe(target) : 'MISSING_IN_DOM';
                        }
                    }
                    // 2) The DOM around the input: walk up 3 ancestors.
                    let region = input;
                    for (let i = 0; i < 3 && region.parentElement; i++) region = region.parentElement;
                    region_html = region.outerHTML.slice(0, 12000);
                    siblings = Array.from(input.parentElement ? input.parentElement.children : [])
                        .filter(el => el !== input).slice(0, 10).map(describe);
                }

                // 3) Every element matching any token, by text OR by attribute value.
                const matches = [];
                const seen = new Set();
                for (const el of Array.from(document.querySelectorAll('*'))) {
                    if (matches.length >= 60) break;
                    if (seen.has(el)) continue;
                    const hay = (clip(el.innerText || el.textContent, 200) + ' ' +
                                 (el.getAttribute('role') || '') + ' ' + el.id + ' ' + classOf(el) + ' ' +
                                 (el.getAttribute('aria-controls') || '') + ' ' +
                                 (el.getAttribute('aria-autocomplete') || '')).toLowerCase();
                    const hit = TOKENS.find(t => t && hay.includes(String(t).toLowerCase()));
                    if (!hit) continue;
                    // Skip huge wrappers (body/main) that trivially contain the text.
                    if (['html', 'body', 'head', 'script', 'style'].includes(el.tagName.toLowerCase())) continue;
                    if ((el.innerText || '').length > 600) continue;
                    seen.add(el);
                    matches.push(Object.assign(describe(el), { matched_token: hit }));
                }

                return {
                    url: location.href,
                    input_selector_matched: matchedSelector,
                    city_input: input ? describe(input) : null,
                    aria: aria,
                    siblings_of_input: siblings,
                    region_html_length: region_html.length,
                    region_html: region_html,
                    matches: matches,
                };
            })()
        """
        script = (script
                  .replace("__TOKENS__", json.dumps(tokens))
                  .replace("__INPUT_SELECTORS__", json.dumps(list(self.CITY_INPUT_SELECTORS))))

        dump = self._safe_eval(page, script, dict, {})
        payload = {
            "portal": self.portal_name,
            "typed_city": city,
            "tokens_searched": tokens,
            "input_selectors_tried": list(self.CITY_INPUT_SELECTORS),
            "suggestion_selectors_tried": list(self.CITY_SUGGESTION_SELECTORS),
            "active_input_selector": input_selector,
            "captured": bool(dump),
            **dump,
        }
        debug_dir = getattr(self, "_active_debug_dir", None)
        if debug_dir is not None:
            try:
                # The region HTML is large: keep it as a separate, openable file.
                region = payload.pop("region_html", "")
                if region:
                    (Path(debug_dir) / "city_input_region.html").write_text(region, encoding="utf-8")
                self._write_debug(Path(debug_dir), "city_autocomplete_debug.json", payload)
            except Exception:
                pass
        return payload

    CITY_FAILURE_REASONS = {
        "city_missing": "No current city of residence was found in the candidate profile or resume.",
        "city_input_not_found": "The Dice 'Current city of residence' input could not be located.",
        "city_suggestion_not_found": "Dice showed no autocomplete suggestions for the typed city.",
        "city_suggestion_click_failed": "The first Dice city suggestion could not be clicked.",
        "city_not_committed": "Dice did not commit the selected city value.",
    }

    def _work_authorization_entry(self, base, label, options, required, resume, job,
                                  candidate_profile, screening_context) -> dict:
        """Resolve Dice Step 2's Work Authorization. Sources, in order:
          1. the shared ScreeningAnswerEngine (saved answers / knowledge base)
          2. settings.dice_default_work_authorization (configuration)
        The resolved value must map onto one of the dropdown's real options; if it
        cannot, this is needs_review -- the automation never invents a legal status
        and never picks an arbitrary option.
        """
        decision = self.screening.decide(
            ScreeningQuestion(text=label, field_type=base["control"], field_kind=base["kind"],
                              options=options, portal="dice"),
            resume=resume, job=job, candidate_profile=candidate_profile, context=screening_context,
        )
        event = decision.to_event() or {}
        event.setdefault("question_text", label)
        event.setdefault("category", decision.category or "work_authorization")

        value, source = None, None
        if not decision.needs_review and decision.answer:
            value, source = str(decision.answer), "screening_engine"
        else:
            configured = getattr(self.settings, "dice_default_work_authorization", None)
            if configured:
                value, source = str(configured), "configured_default"

        if not value:
            event["reason"] = "No saved answer and no configured default work authorization."
            return {**base, "kind": "work_authorization",
                    "action": "needs_review" if required else "skip", "value": None,
                    "value_source": None, "reason": event["reason"], "event": event}

        matched = self._match_option(value, options) if options else value
        if matched is None:
            reason = (f"Work authorization '{value}' does not match any Dice option "
                      f"({', '.join(options[:6])}).")
            event["reason"] = reason
            return {**base, "kind": "work_authorization",
                    "action": "needs_review" if required else "skip", "value": None,
                    "value_source": source, "reason": reason, "event": event}

        event["selected_answer"] = matched
        event["source"] = event.get("source") or source
        return {**base, "kind": "work_authorization", "action": "fill", "value": matched,
                "value_source": source, "resolved_from": value, "event": event}

    def _match_option(self, value: str, options: list[str]) -> str | None:
        """Map a resolved answer onto one of a dropdown's real options. Exact match
        first, then case-insensitive, then containment. Returns None when nothing
        matches -- the caller must then treat the field as needs_review."""
        wanted = (value or "").strip()
        if not wanted:
            return None
        for option in options:
            if option == wanted:
                return option
        lowered = wanted.casefold()
        for option in options:
            if str(option).strip().casefold() == lowered:
                return option
        for option in options:
            text = str(option).strip().casefold()
            if lowered and (lowered in text or text in lowered):
                return option
        return None

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
        submitted: bool | None = False,
    ) -> AutomationResult:
        # `submitted` defaults to False: every non-submit outcome is, by construction, not a
        # submission. Only the live submit path passes True (confirmed) or None (unconfirmed).
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
                "submitted": submitted,
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
                // On the Dice Easy Apply wizard (/job-applications/.../wizard) the SPA
                // renders fields directly into the page, so scan the whole document.
                // On any other (e.g. job-detail) page, only look inside an explicit
                // apply container -- scanning the whole detail page picks up stray
                // controls (a lone checkbox) and yields false "form found" results.
                const isWizard = /\\/job-applications\\//i.test(location.pathname);
                let root;
                if (isWizard) {
                    root = document;
                } else {
                    const forms = document.querySelectorAll(
                        "[data-cy='easy-apply-form'], form[action*='apply'], .apply-form, [role='dialog'], .modal"
                    );
                    if (!forms.length) return [];
                    root = forms[0];
                }
                const controls = Array.from(root.querySelectorAll('input, textarea, select'))
                    .filter(el => {
                        const type = (el.type || '').toLowerCase();
                        if (['hidden', 'submit', 'button'].includes(type)) return false;
                        // Dice hides the real <input type=file> behind a styled dropzone
                        // (0x0), so keep file inputs regardless of their rendered size.
                        if (type === 'file') return true;
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
                    // Dice's cover-letter file input is sr-only with NO label/name/
                    // aria-label -- only aria-describedby="cover letter-description".
                    // Without this fallback it reads as an unlabelled file input and
                    // gets mistaken for the required Resume input.
                    const describedBy = el.getAttribute('aria-describedby');
                    const described = describedBy ? document.getElementById(describedBy) : null;
                    return String((byFor && byFor.innerText) || (wrap && wrap.innerText) || (legend && legend.innerText) || el.getAttribute('aria-label') || el.name || el.placeholder || (described && described.innerText) || describedBy || '').replace(/\\s+/g, ' ').trim();
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
                    // Preserve the true input type so the engine sees numbers,
                    // emails, phones, urls and dates as themselves (not plain text).
                    const numericTypes = ['number', 'range', 'tel', 'email', 'url', 'date', 'month', 'week'];
                    let kind;
                    if (tag === 'select') kind = 'select';
                    else if (tag === 'textarea') kind = 'textarea';
                    else if (type === 'file') kind = 'file';
                    else if (numericTypes.includes(type)) kind = type;
                    else kind = 'text';
                    fields.push({
                        index,
                        label: labelFor(el),
                        control: tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : type),
                        kind,
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
