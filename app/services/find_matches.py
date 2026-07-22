import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings
from app.models import JobOpportunity, ResumeProfile
from app.services.posting_time import (
    UNKNOWN_FRESHNESS,
    extract_posted_phrase,
    freshness_score_for,
    parse_posted_text,
    relative_posted_label,
)


def per_variation_limits(limit_per_query: "int | list[int]", count: int) -> list[int]:
    """Normalize a search budget into one integer per variation. Accepts a single int (uniform)
    or a list aligned with the variations (weighted -- the top variations get a larger candidate
    budget). A short list is padded with its last value and a long one truncated, so a portal
    adapter can accept either form without special-casing. Portal-independent."""
    if isinstance(limit_per_query, (list, tuple)):
        limits = [int(value) for value in limit_per_query]
        if not limits:
            return [0] * count
        if len(limits) < count:
            limits = limits + [limits[-1]] * (count - len(limits))
        return limits[:count]
    return [int(limit_per_query)] * count


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
    # 1-based priority position in the resume-derived search plan (1 = strongest role/skill).
    # Set once in build_search_plan; the completed dashboard groups jobs by this index so a
    # variation's rows stay contiguous, and a job keeps the index of the variation that first
    # discovered it. 0 means "unassigned" (never happens for a planned run).
    variation_index: int = 0
    # Phase-4 role provenance carried from the Phase-3 role_profile (display/debug only; the
    # collection pipeline still keys purely off variation_index and role_priority).
    role_category: str = ""
    domain: str = ""
    job_family: str = ""
    role_rank: int = 0
    synonyms: tuple[str, ...] = ()


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

    # Weight given to the strongest resume-derived variations (1..TOP_VARIATION_COUNT) when
    # sharing the overall Find Matches limit. A soft preference, never a relevance change.
    TOP_VARIATION_COUNT = 5
    TOP_VARIATION_WEIGHT = 2

    def soft_allocation(
        self,
        target_count: int,
        variation_count: int,
        top_n: int | None = None,
        top_weight: int | None = None,
    ) -> list[int]:
        """Soft per-variation share of the overall target, derived from the SAME weighting for
        every requested limit (Find 20/50/100/custom). The top `top_n` variations carry
        `top_weight` (default 2) versus 1 for the rest, so they receive a larger share while
        every variation still gets at least one slot. The total never exceeds `target_count`.
        Purely a collection-effort preference: acceptance still applies the unchanged relevance
        and eligibility rules, so a weak variation is never padded with irrelevant jobs.
        """
        top_n = self.TOP_VARIATION_COUNT if top_n is None else top_n
        top_weight = self.TOP_VARIATION_WEIGHT if top_weight is None else top_weight
        if variation_count <= 0 or target_count <= 0:
            return []
        # Can't give every variation >=1 without exceeding the target; the highest-priority
        # variations (which the caller already lists first) win the available slots.
        count = min(variation_count, target_count)
        weights = [top_weight if i < top_n else 1 for i in range(count)]
        total_weight = sum(weights)
        alloc = [max(1, target_count * weight // total_weight) for weight in weights]
        total = sum(alloc)
        # Trim any overshoot from the LOWEST-priority variations first, never below 1.
        i = count - 1
        while total > target_count and i >= 0:
            cut = min(alloc[i] - 1, total - target_count)
            alloc[i] -= cut
            total -= cut
            i -= 1
        # Hand the rounding remainder to the HIGHEST-priority variations first.
        i = 0
        while total < target_count:
            alloc[i % count] += 1
            total += 1
            i += 1
        return alloc

    def weighted_take(
        self,
        available: list[int],
        target_count: int,
        top_n: int | None = None,
        top_weight: int | None = None,
    ) -> list[int]:
        """How many eligible jobs to KEEP from each variation, given how many each actually has
        (`available`, in priority order). Starts from the weighted soft allocation, caps each by
        real availability, then reallocates any unused budget to the highest-priority variations
        that still have spare eligible jobs. The total never exceeds `target_count`, and when the
        combined eligible supply is short the result is honestly smaller -- nothing is invented.
        """
        n = len(available)
        if n == 0 or target_count <= 0:
            return [0] * n
        soft = self.soft_allocation(target_count, n, top_n, top_weight)
        soft = (soft + [0] * n)[:n]
        take = [min(available[i], soft[i]) for i in range(n)]
        total = sum(take)
        # Reallocate the remaining budget to spare capacity, highest priority first.
        i = 0
        while total < target_count and i < n:
            spare = available[i] - take[i]
            if spare > 0:
                add = min(spare, target_count - total)
                take[i] += add
                total += add
            i += 1
        return take

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

    # Phase-3 category -> the legacy role_priority the collection pipeline and dashboard already use.
    CATEGORY_TO_PRIORITY = {
        "Primary Role": "primary",
        "Strong Fit": "strong_fit",
        "Related": "related",
        "Stretch": "stretch",
    }
    # A tiny controlled expansion for abbreviated canonical titles, so each variation searches ONE
    # real, portal-friendly phrase (never an OR of unrelated roles). Value: (query, [synonyms]).
    CANONICAL_QUERY = {
        "FP&A Analyst": ("Financial Planning and Analysis Analyst", ("FP&A Analyst",)),
        "Finance BI Analyst": ("Finance Business Intelligence Analyst", ("Finance BI Analyst",)),
        "Finance Analytics": ("Finance Analytics Analyst", ()),
        "Business Intelligence Analyst": ("Business Intelligence Analyst", ("BI Analyst",)),
        "BI Developer": ("Business Intelligence Developer", ("BI Developer",)),
        "SOC Analyst": ("Security Operations Center Analyst", ("SOC Analyst",)),
        "IAM Analyst": ("Identity and Access Management Analyst", ("IAM Analyst",)),
        "GRC Analyst": ("Governance Risk and Compliance Analyst", ("GRC Analyst",)),
        "ML Engineer": ("Machine Learning Engineer", ("ML Engineer",)),
        "Software Engineer in Test": ("Software Development Engineer in Test", ("SDET",)),
    }

    def build_search_plan(
        self,
        resume: ResumeProfile,
        location: str,
        manual_keywords: str | None,
    ) -> tuple[list[SearchVariation], dict]:
        """Phase 4: the Phase-3 role_profile is the single source of the ordered search plan for BOTH
        portals. A manual keyword override, or an old saved resume with no usable role_profile, still
        falls back to the legacy plan so nothing breaks for existing users."""
        if (manual_keywords or "").strip():
            return self._legacy_build_search_plan(resume, location, manual_keywords, role_source="manual_override")
        role_profile, role_source = self._resolve_role_profile(resume)
        if role_profile and role_profile.get("roles"):
            return self._plan_from_role_profile(resume, location, role_profile, role_source)
        return self._legacy_build_search_plan(resume, location, manual_keywords, role_source="legacy_fallback")

    def _resolve_role_profile(self, resume: ResumeProfile) -> tuple[dict, str]:
        """Return (role_profile, source). Prefers the persisted Phase-3 profile; for an old saved
        resume without one, regenerates it from the stored resume text; else signals legacy fallback."""
        stored = getattr(resume, "role_profile", None) or {}
        if stored.get("roles"):
            return stored, "role_profile"
        raw_text = (getattr(resume, "raw_text", "") or "").strip()
        if raw_text:
            try:
                from app.services.resume_parser import ResumeParser

                regenerated = ResumeParser(self.settings).parse(raw_text).get("role_profile") or {}
                if regenerated.get("roles"):
                    return regenerated, "regenerated"
            except Exception:
                pass
        return {}, "legacy_fallback"

    def _role_query(self, title: str) -> tuple[str, tuple[str, ...]]:
        query, synonyms = self.CANONICAL_QUERY.get(title, (title, ()))
        return query, synonyms

    def _plan_from_role_profile(
        self,
        resume: ResumeProfile,
        location: str,
        role_profile: dict,
        role_source: str,
    ) -> tuple[list[SearchVariation], dict]:
        """One narrow variation per Phase-3 role, in ranked order: variation 1 = Primary, 2-5 =
        Strong Fit, 6-9 = Related, 10-12 = Stretch (fewer for provisional domains -- never padded)."""
        roles = role_profile.get("roles") or []
        locations = self.expand_locations(location)
        primary_location = locations[0]["location"]
        primary_role = roles[0].get("title") if roles else (resume.detected_role or "")

        queries: list[SearchVariation] = []
        for role in roles[:24]:
            title = str(role.get("title") or "").strip()
            if not title:
                continue
            query, synonyms = self._role_query(title)
            category = str(role.get("category") or "")
            priority = self.CATEGORY_TO_PRIORITY.get(category, "related")
            queries.append(
                SearchVariation(
                    query=query,
                    location=primary_location,
                    role=title,
                    expanded_role=title,
                    pass_name=f"{priority}_role_primary_location",
                    search_role=title,
                    role_priority=priority,
                    role_confidence=float(role.get("confidence") or 0),
                    role_category=category,
                    domain=str(role.get("domain") or ""),
                    job_family=str(role.get("job_family") or ""),
                    role_rank=int(role.get("rank") or 0),
                    synonyms=tuple(synonyms),
                )
            )
        # Deterministic, 1-based variation numbering in ranked order -- the permanent group key.
        queries = [replace(query, variation_index=index + 1) for index, query in enumerate(queries)]
        plan = {
            "role_source": role_source,
            "taxonomy_status": role_profile.get("taxonomy_status", "supported"),
            "primary_role": primary_role,
            "roles": roles,
            "role_groups": [
                {"role": v.role, "confidence": v.role_confidence, "role_priority": v.role_priority, "role_rank": v.role_rank}
                for v in queries
            ],
            "searchable_role_groups": [],
            "expanded_roles": roles,
            "expanded_skills": [],
            "locations": locations,
            "queries": [asdict(query) for query in queries],
            "display_query": primary_role,
        }
        return queries, plan

    def _legacy_build_search_plan(
        self,
        resume: ResumeProfile,
        location: str,
        manual_keywords: str | None,
        role_source: str = "legacy",
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
        # Freeze the priority order: the 1-based index of each variation in this list is its
        # permanent group key on the completed dashboard. Assigned here (once), never inferred
        # later from a job title.
        queries = [replace(query, variation_index=index + 1) for index, query in enumerate(queries[:24])]
        plan = {
            "role_source": role_source,
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

    def enrich_raw_job(
        self,
        raw: dict,
        variation: SearchVariation,
        reference_time: datetime | None = None,
    ) -> dict:
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
        # Posting-time normalization. One reference timestamp per Find Matches run (the
        # caller passes the run's search_timestamp) so every relative phrase in the run is
        # measured against the same instant. The portal's own posted text is authoritative;
        # when the adapter did not isolate one, the phrase is rescued from the card text.
        # Unknown stays unknown -- discovery time is never substituted for posting time.
        reference = reference_time or datetime.utcnow()
        metadata = dict(raw.get("job_description_metadata") or {})
        posted_raw = str(metadata.get("posted_date") or raw.get("posted_date") or "").strip()
        # PRECEDENCE: an adapter that supplied a posted_date field (even an empty one) has
        # already done SCOPED, portal-aware extraction -- its verdict is final, including
        # "no reliable value" (Unknown). The loose text rescue below runs ONLY for raw jobs
        # that never attempted extraction at all; free-scanning a card/description here can
        # promote prose ("apply today") or a neighboring "Updated" age into a posting date.
        adapter_supplied = "posted_date" in metadata or "posted_date" in raw
        if not posted_raw and not adapter_supplied:
            posted_raw = extract_posted_phrase(location, description)
        # An adapter-supplied EXACT timestamp (Dice's embedded postedDate) outranks the
        # display text for normalization; the display text keeps the portal's visible wording.
        posted_iso = str(metadata.get("posted_iso") or "").strip()
        posted_at = None
        if posted_iso:
            posted_at = parse_posted_text(posted_iso, reference)
        if posted_at is None:
            posted_at = parse_posted_text(posted_raw, reference)
        metadata["posted_date"] = posted_raw
        metadata["posted_at"] = posted_at.isoformat() if posted_at else None
        # DISPLAY value (never used for sorting). Dice's card badge is coarse and often stale
        # ("Today" on a 2-day-old job) and its payload fallback is a raw ISO string, so the
        # visible age is derived from the authoritative posted_at against this run's reference
        # -- keeping the label consistent with the newest-to-oldest ordering. Other portals
        # (LinkedIn) already expose correct relative text; theirs is preserved verbatim.
        if portal == "dice":
            metadata["posted_display"] = relative_posted_label(posted_at, reference)
        else:
            metadata["posted_display"] = posted_raw
        # The variation that DISCOVERED this job (its 1-based priority). The completed dashboard
        # groups strictly by this index, so it must be the owning variation, not re-inferred.
        metadata["variation_index"] = variation.variation_index or None
        enriched = {
            **raw,
            **complexity,
            "freshness_score": freshness_score_for(posted_at, reference),
            "job_description_metadata": metadata,
            "search_query_used": variation.query,
            "role_detected": variation.role,
            "expanded_role": variation.expanded_role,
            "search_role": variation.search_role or variation.expanded_role or variation.role,
            "role_priority": variation.role_priority,
            "priority_reason": "",
            "match_reason": "",
        }
        # Raw posting text lives in the metadata, not as a JobOpportunity column.
        enriched.pop("posted_date", None)
        return enriched

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

    def estimate_freshness(self, text: str, reference_time: datetime | None = None) -> dict:
        """Freshness from loose text, via the shared deterministic posting-time parser (one
        authority for all portals). Unknown text scores UNKNOWN_FRESHNESS -- below every
        confirmed date -- and no date is ever invented."""
        reference = reference_time or datetime.utcnow()
        posted_at = parse_posted_text(extract_posted_phrase(text), reference)
        if posted_at is None:
            return {"score": UNKNOWN_FRESHNESS, "label": "Unknown"}
        age_hours = max(0.0, (reference - posted_at).total_seconds() / 3600.0)
        if age_hours <= 24:
            label = "Today"
        elif age_hours <= 48:
            label = "Yesterday"
        elif age_hours <= 24 * 7:
            label = "Last week"
        else:
            label = "Older"
        return {"score": freshness_score_for(posted_at, reference), "label": label}

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
        """The job_url to PERSIST: always the portal's own resolved URL, never a manufactured
        one.

        This used to append a stable ``#job-<hash>`` fragment to shared search-results URLs so
        distinct jobs would not collide on UNIQUE (job_url, resume_id, search_batch_id). That
        made the rows insertable but left them pointing at the same search page, so Apply
        opened a results list and applied to whichever job was selected there. A portal now
        resolves a real per-job URL or reports the job URL_UNRESOLVED and drops it, so there is
        no shared URL left to disambiguate -- and nothing here may invent identity."""
        return str(raw.get("job_url") or "")

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
