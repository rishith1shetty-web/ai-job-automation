import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings
from app.models import JobOpportunity, ResumeProfile


@dataclass(frozen=True)
class SearchVariation:
    query: str
    location: str
    role: str
    expanded_role: str
    pass_name: str
    search_role: str = ""
    role_priority: str = "related"
    role_confidence: float = 0


class FindMatchesEngine:
    DEFAULT_TARGET = 100
    TARGET_MAX = 100

    ROLE_EXPANSIONS = {
        "Data Analyst": ["Junior Data Analyst", "Business Analyst", "BI Analyst", "Reporting Analyst", "SQL Analyst"],
        "Business Intelligence Analyst": ["BI Analyst", "Power BI Analyst", "Dashboard Developer", "Reporting Analyst"],
        "Data Scientist": ["Machine Learning Engineer", "Analytics Engineer", "AI Engineer", "Data Analyst"],
        "Machine Learning Engineer": ["ML Engineer", "AI Engineer", "Computer Vision Engineer", "Data Scientist"],
        "Analytics Engineer": ["Data Engineer", "Data Analyst", "BI Analyst", "SQL Developer"],
        "Data Engineer": ["ETL Developer", "Analytics Engineer", "SQL Developer", "Python Data Engineer"],
        "Backend Developer": ["Python Developer", "FastAPI Developer", "API Developer", "Software Engineer"],
        "Python Developer": ["Backend Developer", "FastAPI Developer", "Software Engineer"],
        "Software Engineer": ["Python Developer", "Backend Developer", "Junior Software Engineer"],
    }
    SKILL_EXPANSIONS = {
        "sql": ["SQL Analyst", "MySQL", "PostgreSQL", "SQL Server"],
        "python": ["Python Developer", "Pandas", "NumPy", "Scikit-learn", "FastAPI"],
        "power bi": ["Power BI Analyst", "BI Analyst", "Dashboard Developer"],
        "tableau": ["Tableau Developer", "BI Analyst", "Reporting Analyst"],
        "machine learning": ["Machine Learning Engineer", "ML Engineer", "AI Engineer"],
        "fastapi": ["FastAPI Developer", "Backend Developer", "API Developer"],
        "mongodb": ["Backend Developer", "Data Engineer"],
        "excel": ["Data Analyst", "Reporting Analyst", "Business Analyst"],
    }
    ROLE_SIGNALS = {
        "Data Analyst": ["data analyst", "excel", "sql", "power bi", "tableau", "reporting", "dashboard"],
        "Business Intelligence Analyst": ["business intelligence", "bi analyst", "power bi", "tableau", "dashboard", "reporting"],
        "Data Scientist": ["data scientist", "machine learning", "statistics", "predictive", "model", "python"],
        "Machine Learning Engineer": ["machine learning", "ml engineer", "tensorflow", "pytorch", "scikit", "computer vision", "nlp"],
        "Analytics Engineer": ["analytics engineer", "dbt", "etl", "data warehouse", "sql", "pipeline"],
        "Data Engineer": ["data engineer", "etl", "pipeline", "spark", "snowflake", "airflow", "data warehouse"],
        "Backend Developer": ["backend", "fastapi", "django", "flask", "api", "rest", "postgresql"],
        "Python Developer": ["python developer", "python", "fastapi", "django", "flask"],
        "Software Engineer": ["software engineer", "developer", "programming", "api", "python", "javascript"],
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def create_debug_dir(self) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        debug_dir = self.settings.find_matches_debug_dir / timestamp
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def write_debug(self, debug_dir: Path, filename: str, payload) -> None:
        (debug_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def target_count(self, requested_limit: int) -> int:
        default_limit = int(getattr(self.settings, "find_matches_default_limit", self.DEFAULT_TARGET) or self.DEFAULT_TARGET)
        max_limit = int(getattr(self.settings, "find_matches_max_results", self.TARGET_MAX) or self.TARGET_MAX)
        requested = int(requested_limit or default_limit)
        return max(1, min(max_limit, requested))

    def collection_plan(self, target_count: int, variation_count: int) -> dict:
        selected_variation_count = min(
            variation_count,
            max(4, min(24, (target_count + 6) // 7)),
        )
        selected_variation_count = max(1, selected_variation_count)
        limit_per_query = max(5, min(12, (target_count + selected_variation_count - 1) // selected_variation_count + 2))
        return {
            "selected_variation_count": selected_variation_count,
            "limit_per_query": limit_per_query,
        }

    def detect_roles(self, resume: ResumeProfile, manual_keywords: str | None = None) -> list[dict]:
        manual = (manual_keywords or "").strip()
        if manual:
            return [{"role": manual, "confidence": 100, "reason": "Manual override"}]
        suggestions = []
        for item in resume.role_suggestions or []:
            role = (item.get("role") or item.get("title") or "").strip()
            if role:
                suggestions.append(
                    {
                        "role": role,
                        "confidence": min(float(item.get("confidence", 70)), 92),
                        "reason": "Resume role suggestion",
                    }
                )
        if resume.detected_role:
            suggestions.append(
                {
                    "role": resume.detected_role,
                    "confidence": min(float(resume.role_confidence or 72), 88),
                    "reason": "Detected primary role",
                }
            )
        profile_text = self._profile_text(resume)
        for role, signals in self.ROLE_SIGNALS.items():
            hits = [signal for signal in signals if signal in profile_text]
            if hits:
                confidence = min(55 + len(hits) * 8, 86)
                suggestions.append({"role": role, "confidence": confidence, "reason": ", ".join(hits[:5])})
        return self._dedupe_roles(suggestions)[:10] or [
            {"role": "Software Engineer", "confidence": 50, "reason": "Fallback role"}
        ]

    def role_groups(self, roles: list[dict]) -> list[dict]:
        grouped: list[dict] = []
        for index, item in enumerate(roles):
            confidence = float(item.get("confidence") or 0)
            if index == 0:
                priority = "primary"
            elif confidence >= 70:
                priority = "strong_fit"
            elif confidence >= 60:
                priority = "related"
            else:
                priority = "stretch"
            grouped.append({**item, "role_priority": priority, "role_rank": index + 1})
        return grouped

    def expand_roles(self, roles: list[dict]) -> list[dict]:
        expanded: list[dict] = []
        for role_item in roles[:10]:
            role = role_item["role"]
            expanded.append({**role_item, "expanded_from": role})
            for expansion in self.ROLE_EXPANSIONS.get(role, [])[:3]:
                expanded.append(
                    {
                        "role": expansion,
                        "confidence": max(float(role_item["confidence"]) - 8, 45),
                        "reason": f"Variation of {role}",
                        "expanded_from": role,
                    }
                )
        return self._dedupe_roles(expanded)[:12]

    def searchable_role_groups(self, grouped_roles: list[dict]) -> list[dict]:
        domain_specific_roles = {
            "Healthcare Data Analyst",
            "Clinical Data Analyst",
            "Healthcare BI Analyst",
            "Computer Vision Engineer",
            "NLP Engineer",
        }
        searchable: list[dict] = []
        for role in grouped_roles:
            confidence = float(role.get("confidence") or 0)
            if role.get("role_priority") != "stretch":
                searchable.append(role)
            elif role.get("role") in domain_specific_roles and confidence >= 58:
                searchable.append(role)
            elif role.get("role") not in domain_specific_roles and confidence >= 50:
                searchable.append(role)
        return searchable or grouped_roles[:1]

    def expand_skills(self, resume: ResumeProfile) -> list[dict]:
        source_skills = [str(skill).strip() for skill in (resume.skills or []) + (resume.technologies or []) + (resume.keywords or [])]
        expanded: list[dict] = []
        seen: set[str] = set()
        for skill in source_skills:
            key = skill.lower()
            if key in self.SKILL_EXPANSIONS and key not in seen:
                seen.add(key)
                expanded.append({"skill": skill, "expansions": self.SKILL_EXPANSIONS[key][:4]})
            if len(expanded) >= 5:
                break
        return expanded

    def expand_locations(self, location: str) -> list[dict]:
        primary = (location or "United States").strip()
        lowered = primary.lower()
        nearby: list[str] = []
        if "mumbai" in lowered:
            nearby = ["Navi Mumbai", "Thane", "Pune", "Remote", "Hybrid"]
        elif "pune" in lowered:
            nearby = ["Mumbai", "Remote", "Hybrid"]
        elif "bangaluru" in lowered or "bangalore" in lowered:
            nearby = ["Bengaluru", "Remote", "Hybrid"]
        elif lowered == "remote":
            nearby = ["Remote"]
        else:
            nearby = ["Remote", "Hybrid"]
        locations = [primary] + [item for item in nearby if item.lower() != lowered]
        return [{"location": item, "reason": "Primary" if index == 0 else "Limited expansion"} for index, item in enumerate(locations[:6])]

    def build_search_plan(
        self,
        resume: ResumeProfile,
        location: str,
        manual_keywords: str | None,
    ) -> tuple[list[SearchVariation], dict]:
        roles = self.detect_roles(resume, manual_keywords)
        expanded_roles = self.expand_roles(roles)
        skills = self.expand_skills(resume)
        locations = self.expand_locations(location)
        primary_role = roles[0]["role"]
        primary_location = locations[0]["location"]
        grouped_roles = self.role_groups(roles)
        searchable_grouped_roles = self.searchable_role_groups(grouped_roles)

        queries: list[SearchVariation] = []
        for role in searchable_grouped_roles:
            self._add_query(
                queries,
                role["role"],
                primary_location,
                primary_role,
                role["role"],
                f"{role['role_priority']}_role_primary_location",
                search_role=role["role"],
                role_priority=role["role_priority"],
                role_confidence=float(role.get("confidence") or 0),
            )
        for role in expanded_roles:
            if role["role"].lower() in {item["role"].lower() for item in searchable_grouped_roles}:
                continue
            origin = next(
                (
                    item
                    for item in searchable_grouped_roles
                    if item["role"].lower() == str(role.get("expanded_from", "")).lower()
                ),
                None,
            )
            if origin is None:
                continue
            self._add_query(
                queries,
                role["role"],
                primary_location,
                primary_role,
                role["role"],
                f"{origin['role_priority']}_role_variant_primary_location",
                search_role=origin["role"],
                role_priority=origin["role_priority"],
                role_confidence=float(origin.get("confidence") or 0),
            )
        for skill_group in skills[:4]:
            for skill_query in skill_group["expansions"][:2]:
                if any(word in skill_query.lower() for word in ["developer", "analyst", "engineer"]):
                    self._add_query(
                        queries,
                        skill_query,
                        primary_location,
                        primary_role,
                        skill_query,
                        "skill_expansion_primary_location",
                        search_role=primary_role,
                        role_priority="related",
                        role_confidence=float(grouped_roles[0].get("confidence") or 0),
                    )
        for location_item in locations[1:6]:
            self._add_query(
                queries,
                primary_role,
                location_item["location"],
                primary_role,
                primary_role,
                "primary_role_nearby_location",
                search_role=primary_role,
                role_priority="primary",
                role_confidence=float(grouped_roles[0].get("confidence") or 0),
            )
        for role in searchable_grouped_roles[1:5]:
            for location_item in locations[1:3]:
                self._add_query(
                    queries,
                    role["role"],
                    location_item["location"],
                    primary_role,
                    role["role"],
                    f"{role['role_priority']}_role_nearby_location",
                    search_role=role["role"],
                    role_priority=role["role_priority"],
                    role_confidence=float(role.get("confidence") or 0),
                )
        if not manual_keywords and resume.generated_search_query:
            self._add_query(
                queries,
                resume.generated_search_query,
                primary_location,
                primary_role,
                primary_role,
                "resume_generated_query_fallback",
                search_role=primary_role,
                role_priority="fallback",
                role_confidence=float(grouped_roles[0].get("confidence") or 0),
            )
        queries = queries[:24]
        plan = {
            "roles": roles,
            "role_groups": grouped_roles,
            "searchable_role_groups": searchable_grouped_roles,
            "expanded_roles": expanded_roles,
            "expanded_skills": skills,
            "locations": locations,
            "queries": [asdict(query) for query in queries],
            "display_query": " OR ".join(role["role"] for role in roles[:5]),
        }
        return queries, plan

    def enrich_raw_job(self, raw: dict, variation: SearchVariation) -> dict:
        description = raw.get("description") or ""
        title = raw.get("title") or ""
        location = raw.get("location") or ""
        portal = str(raw.get("portal") or "").lower()
        complexity = self.estimate_complexity(
            title,
            description,
            job_url=raw.get("job_url") or "",
            easy_apply=bool(raw.get("easy_apply")),
        )
        if portal == "dice":
            complexity["form_provider"] = self.dice_form_provider_label(raw, complexity.get("form_provider"))
        freshness = self.estimate_freshness(location + " " + description)
        return {
            **raw,
            **complexity,
            "freshness_score": freshness["score"],
            "search_query_used": variation.query,
            "role_detected": variation.role,
            "expanded_role": variation.expanded_role,
            "search_role": variation.search_role or variation.expanded_role or variation.role,
            "role_priority": variation.role_priority,
            "priority_reason": "",
            "match_reason": "",
        }

    def dice_form_provider_label(self, raw: dict, fallback: str | None = None) -> str:
        provider = str(raw.get("form_provider") or fallback or "").strip().lower()
        text = " ".join(
            str(raw.get(field) or "")
            for field in ("title", "company", "location", "description", "job_url")
        ).lower()
        metadata = raw.get("job_description_metadata") or {}
        apply_url = str(metadata.get("apply_url") or raw.get("apply_url") or "").lower()
        easy_apply = bool(raw.get("easy_apply"))
        if "easy" in provider or "easy apply" in text or "quick apply" in text:
            return "Dice Easy Apply"
        if easy_apply or "quick apply" in text:
            return "Dice Easy Apply"
        if "apply" in provider or "apply" in text or "apply" in apply_url:
            return "Dice Apply"
        if provider in {"dice", "dice.com"}:
            return "Dice"
        return "Unknown Dice Form"

    def apply_priority_scores(self, jobs: list[JobOpportunity]) -> list[JobOpportunity]:
        for job in jobs:
            complexity_score = float(job.complexity_score or 50)
            completion_likelihood = float(job.completion_likelihood_score or complexity_score or 50)
            freshness_score = float(job.freshness_score or 50)
            easy_apply_verified = 100 if job.easy_apply else 0
            role_priority_bonus = {
                "primary": 8,
                "strong_fit": 5,
                "related": 2,
                "stretch": -2,
                "fallback": -3,
            }.get((job.role_priority or "related").lower(), 0)
            priority = (
                float(job.match_score or 0) * 0.45
                + completion_likelihood * 0.35
                + freshness_score * 0.10
                + easy_apply_verified * 0.10
                + role_priority_bonus
            )
            job.priority_score = round(priority, 2)
            job.match_reason = job.match_rationale or "Resume and job text similarity."
            job.priority_reason = (
                f"{job.match_score:.1f}% match, {(job.role_priority or 'related').replace('_', ' ')} search role, "
                f"{job.complexity_level or 'unknown'} complexity, "
                f"{completion_likelihood:.0f}% completion likelihood, {freshness_score:.0f} freshness, "
                f"Easy Apply {'verified' if job.easy_apply else 'not verified'}."
            )
            reasons = ", ".join((job.friction_reasons or [])[:4]) or "low-friction Easy Apply flow"
            job.apply_priority_reason = (
                f"{job.recommended_apply_priority or 'Medium'} priority: "
                f"{completion_likelihood:.0f}% likely to complete; {reasons}."
            )
        return sorted(
            jobs,
            key=lambda item: (
                item.priority_score or 0,
                item.completion_likelihood_score or 0,
                bool(item.easy_apply),
                -(item.estimated_steps or 99),
                item.match_score or 0,
            ),
            reverse=True,
        )

    def estimate_complexity(self, title: str, description: str, job_url: str = "", easy_apply: bool = True) -> dict:
        text = f"{title}\n{description}\n{job_url}".lower()
        provider = self.detect_form_provider(job_url, description)
        if easy_apply and provider in {"LinkedIn", "Custom/Unknown"}:
            provider = "LinkedIn Easy Apply"
        provider_risk_map = {
            "LinkedIn Easy Apply": 0,
            "LinkedIn": 8,
            "Workable": 34,
            "Greenhouse": 32,
            "Bullhorn": 36,
            "Lever": 28,
            "Ashby": 28,
            "SmartRecruiters": 30,
            "Custom/Unknown": 40,
        }
        screening_count = len(re.findall(r"\b(required|minimum|must have|question|questions|screening|years of|experience with|how many years)\b", text))
        skill_year_questions = len(re.findall(r"\byears?\s+of\s+(?:microsoft\s+)?(?:excel|sql|python|databases?|power\s*bi|tableau)\b", text))
        simple_yes_no = len(re.findall(r"\b(yes/no|yes or no|are you willing|are you comfortable|do you have|have you completed)\b", text))
        free_text = len(re.findall(r"\b(cover letter|essay|explain|describe|why are you|long answer|short answer|writing sample)\b", text))
        salary = bool(re.search(r"\b(salary|compensation|ctc|hourly|pay range)\b", text))
        legal = bool(re.search(r"\b(visa|sponsorship|authorized|work authorization|background check|green card|citizen|security clearance)\b", text))
        protected = bool(re.search(r"\b(disability|veteran|race|gender|ethnicity|demographic)\b", text))
        profile_link = bool(re.search(r"\b(linkedin profile|portfolio|github|website url|personal website)\b", text))
        employment = bool(re.search(r"\b(employment history|work history|previous employer|start date|end date)\b", text))
        cover_letter = bool(re.search(r"\bcover letter\b", text))
        unsupported_field_count = (
            free_text
            + int(salary)
            + int(legal)
            + int(protected)
            + int(profile_link)
            + int(employment)
            + int(cover_letter)
        )
        required_fields = min(screening_count + unsupported_field_count, 20)
        estimated_steps = 1 + min(required_fields // 3, 4) + int(employment)
        external_form_risk = provider_risk_map.get(provider, 40)
        if not easy_apply:
            external_form_risk = max(external_form_risk, 45)
        manual_review_risk = min(
            100,
            free_text * 12
            + int(salary) * 10
            + int(legal) * 18
            + int(protected) * 20
            + int(profile_link) * 10
            + int(employment) * 16
            + max(0, screening_count - skill_year_questions - simple_yes_no) * 5,
        )
        friction = min(
            100,
            required_fields * 4
            + unsupported_field_count * 7
            + external_form_risk
            + manual_review_risk * 0.35
            - min(skill_year_questions, 5) * 3
            - min(simple_yes_no, 5) * 2,
        )
        friction = max(0, friction)
        completion_likelihood = round(max(0, min(100, 100 - friction)), 2)
        score = completion_likelihood
        if score >= 75:
            level = "Simple"
        elif score >= 45:
            level = "Medium"
        else:
            level = "Complex"
        reasons: list[str] = []
        if provider != "LinkedIn Easy Apply":
            reasons.append(f"{provider} form provider risk")
        if screening_count:
            reasons.append(f"{min(screening_count, 20)} estimated screening questions")
        if cover_letter:
            reasons.append("cover letter may be required")
        if profile_link:
            reasons.append("profile/link field may be required")
        if legal:
            reasons.append("legal/work authorization question risk")
        if protected:
            reasons.append("protected demographic field risk")
        if free_text:
            reasons.append("long text/custom answer field risk")
        if employment:
            reasons.append("employment history form risk")
        if not reasons:
            reasons.append("simple native Easy Apply signals")
        if completion_likelihood >= 75:
            recommended = "Simple"
        elif completion_likelihood >= 45:
            recommended = "Medium"
        else:
            recommended = "Complex"
        return {
            "complexity_score": round(score, 2),
            "complexity_level": level,
            "estimated_steps": max(1, estimated_steps),
            "screening_question_count": min(screening_count, 20),
            "required_fields": required_fields,
            "employment_history_required": employment,
            "application_friction_score": round(friction, 2),
            "completion_likelihood_score": completion_likelihood,
            "unsupported_field_count": unsupported_field_count,
            "external_form_risk": round(external_form_risk, 2),
            "manual_review_risk": round(manual_review_risk, 2),
            "form_provider": provider,
            "friction_reasons": reasons,
            "recommended_apply_priority": recommended,
            "apply_priority_reason": f"{recommended}: {completion_likelihood:.0f}% likely to complete; {', '.join(reasons[:4])}.",
        }

    def detect_form_provider(self, job_url: str, description: str = "") -> str:
        text = f"{job_url}\n{description}".lower()
        if "workable" in text:
            return "Workable"
        if "greenhouse" in text:
            return "Greenhouse"
        if "bullhorn" in text:
            return "Bullhorn"
        if "lever.co" in text or "jobs.lever" in text:
            return "Lever"
        if "ashby" in text:
            return "Ashby"
        if "smartrecruiters" in text:
            return "SmartRecruiters"
        if "linkedin.com" in text:
            return "LinkedIn Easy Apply" if "easy apply" in description.lower() or "/jobs/view/" in (job_url or "") else "LinkedIn"
        if job_url:
            return "Custom/Unknown"
        return "LinkedIn Easy Apply"

    def estimate_freshness(self, text: str) -> dict:
        lowered = text.lower()
        if "today" in lowered or "just now" in lowered:
            return {"score": 100, "label": "Today"}
        if "hour" in lowered or "24 hours" in lowered:
            return {"score": 92, "label": "Last 24 hours"}
        if re.search(r"\b[123]\s+days?\s+ago\b", lowered):
            return {"score": 82, "label": "Last 3 days"}
        if "week" in lowered or re.search(r"\b[4-7]\s+days?\s+ago\b", lowered):
            return {"score": 65, "label": "Last week"}
        if "month" in lowered:
            return {"score": 25, "label": "Older"}
        return {"score": 50, "label": "Unknown"}

    # Last-path-segment tokens that name a listing/search page, NOT a single job. When a
    # portal cannot resolve a per-job URL and falls back to the shared search-results URL
    # (e.g. Dice's "https://www.dice.com/jobs?..."), the stripped URL and its last path
    # segment are IDENTICAL for every job in the list. Emitting them as identity keys would
    # collapse every distinct job onto the first one. Real per-job URLs (LinkedIn
    # /jobs/view/<id>, Dice /job-detail/<guid>) never hit this set, so their identity is
    # unchanged -- this only rescues the fallback-URL case.
    NON_IDENTIFYING_URL_TOKENS = {"", "jobs", "job", "search", "job-search", "jobsearch", "results"}

    def duplicate_key(self, raw: dict) -> tuple[str, ...]:
        job_url = str(raw.get("job_url") or "").split("?")[0].rstrip("/")
        job_id = self.linkedin_job_id(job_url)
        title = self._normalize(raw.get("title") or "")
        company = self._normalize(raw.get("company") or "")
        location = self._normalize(raw.get("location") or "")
        content_key = f"{title}|{company}|{location}"
        # A search-results / listing URL is not a per-job identity. Drop the URL-based keys so
        # distinct jobs that only share the fallback URL are not treated as duplicates; identity
        # then comes from the (title, company, location) content key, which stays distinct.
        if job_id.lower() in self.NON_IDENTIFYING_URL_TOKENS:
            return (content_key,)
        return (job_url, job_id, content_key)

    def storage_job_url(self, raw: dict) -> str:
        """The job_url to PERSIST. The job_opportunities table enforces UNIQUE
        (job_url, resume_id, search_batch_id). When a portal could not resolve a per-job URL
        and fell back to the shared search-results URL, every distinct job in a batch carries
        the SAME job_url and would collide on that constraint -- so the whole insert flush
        rolls back and nothing persists. Disambiguate such fallback URLs with a stable per-job
        fragment derived from the job's own content identity (the same identity dedup uses), so
        every distinct job gets a distinct stored URL. Real per-job URLs are returned unchanged.
        The fragment is a URL no-op (ignored on navigation), so the apply/URL-resolution flow is
        unaffected."""
        url = str(raw.get("job_url") or "")
        stripped = url.split("?")[0].rstrip("/")
        if self.linkedin_job_id(stripped).lower() not in self.NON_IDENTIFYING_URL_TOKENS:
            return url
        title = self._normalize(raw.get("title") or "")
        company = self._normalize(raw.get("company") or "")
        location = self._normalize(raw.get("location") or "")
        identity = f"{title}|{company}|{location}"
        if not identity.strip("|") or not url:
            return url
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
        return f"{url}#job-{digest}"

    def linkedin_job_id(self, job_url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)", job_url or "")
        if match:
            return match.group(1)
        parsed = urlparse(job_url or "")
        return parsed.path.rstrip("/").split("/")[-1] if parsed.path else ""

    def _profile_text(self, resume: ResumeProfile) -> str:
        pieces = [
            resume.raw_text or "",
            " ".join(resume.skills or []),
            " ".join(resume.technologies or []),
            " ".join(resume.keywords or []),
            " ".join(resume.job_titles or []),
            " ".join(resume.domain_expertise or []),
            " ".join(str(item) for item in resume.experience or []),
            " ".join(str(item) for item in resume.education or []),
        ]
        return " ".join(pieces).lower()

    def _dedupe_roles(self, roles: list[dict]) -> list[dict]:
        by_role: dict[str, dict] = {}
        for item in roles:
            role = item["role"].strip()
            key = role.lower()
            if key not in by_role or float(item.get("confidence", 0)) > float(by_role[key].get("confidence", 0)):
                by_role[key] = {**item, "role": role, "confidence": round(float(item.get("confidence", 0)), 2)}
        return sorted(by_role.values(), key=lambda item: item["confidence"], reverse=True)

    def _add_query(
        self,
        queries: list[SearchVariation],
        query: str,
        location: str,
        role: str,
        expanded_role: str,
        pass_name: str,
        search_role: str | None = None,
        role_priority: str = "related",
        role_confidence: float = 0,
    ) -> None:
        query = re.sub(r"\s+", " ", (query or "").strip())
        if not query:
            return
        candidate = SearchVariation(
            query=query,
            location=location,
            role=role,
            expanded_role=expanded_role,
            pass_name=pass_name,
            search_role=search_role or role,
            role_priority=role_priority,
            role_confidence=round(float(role_confidence or 0), 2),
        )
        key = (candidate.query.lower(), candidate.location.lower())
        if key not in {(item.query.lower(), item.location.lower()) for item in queries}:
            queries.append(candidate)

    def _normalize(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
