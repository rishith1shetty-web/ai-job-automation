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


class FindMatchesEngine:
    TARGET_MIN = 15
    TARGET_MAX = 20

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
        return max(self.TARGET_MIN, min(self.TARGET_MAX, requested_limit or self.TARGET_MAX))

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
        return self._dedupe_roles(suggestions)[:5] or [
            {"role": "Software Engineer", "confidence": 50, "reason": "Fallback role"}
        ]

    def expand_roles(self, roles: list[dict]) -> list[dict]:
        expanded: list[dict] = []
        for role_item in roles[:5]:
            role = role_item["role"]
            expanded.append({**role_item, "expanded_from": role})
            for expansion in self.ROLE_EXPANSIONS.get(role, [])[:5]:
                expanded.append(
                    {
                        "role": expansion,
                        "confidence": max(float(role_item["confidence"]) - 8, 45),
                        "reason": f"Variation of {role}",
                        "expanded_from": role,
                    }
                )
        return self._dedupe_roles(expanded)[:12]

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

        queries: list[SearchVariation] = []
        self._add_query(queries, primary_role, primary_location, primary_role, primary_role, "primary_role_primary_location")
        if not manual_keywords and resume.generated_search_query:
            self._add_query(queries, resume.generated_search_query, primary_location, primary_role, primary_role, "resume_generated_query")
        for role in expanded_roles[1:6]:
            self._add_query(queries, role["role"], primary_location, primary_role, role["role"], "expanded_role_primary_location")
        for skill_group in skills[:4]:
            for skill_query in skill_group["expansions"][:2]:
                if any(word in skill_query.lower() for word in ["developer", "analyst", "engineer"]):
                    self._add_query(queries, skill_query, primary_location, primary_role, skill_query, "skill_expansion_primary_location")
        for location_item in locations[1:6]:
            self._add_query(queries, primary_role, location_item["location"], primary_role, primary_role, "primary_role_nearby_location")
        for role in expanded_roles[1:4]:
            for location_item in locations[1:3]:
                self._add_query(queries, role["role"], location_item["location"], primary_role, role["role"], "expanded_role_nearby_location")
        queries = queries[:18]
        plan = {
            "roles": roles,
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
        complexity = self.estimate_complexity(title, description)
        freshness = self.estimate_freshness(location + " " + description)
        return {
            **raw,
            **complexity,
            "freshness_score": freshness["score"],
            "search_query_used": variation.query,
            "role_detected": variation.role,
            "expanded_role": variation.expanded_role,
            "priority_reason": "",
            "match_reason": "",
        }

    def apply_priority_scores(self, jobs: list[JobOpportunity]) -> list[JobOpportunity]:
        for job in jobs:
            complexity_score = float(job.complexity_score or 50)
            freshness_score = float(job.freshness_score or 50)
            easy_apply_verified = 100 if job.easy_apply else 0
            priority = (
                float(job.match_score or 0) * 0.40
                + complexity_score * 0.30
                + freshness_score * 0.20
                + easy_apply_verified * 0.10
            )
            job.priority_score = round(priority, 2)
            job.match_reason = job.match_rationale or "Resume and job text similarity."
            job.priority_reason = (
                f"{job.match_score:.1f}% match, {job.complexity_level or 'unknown'} complexity, "
                f"{freshness_score:.0f} freshness, Easy Apply {'verified' if job.easy_apply else 'not verified'}."
            )
        return sorted(
            jobs,
            key=lambda item: (
                item.priority_score or 0,
                bool(item.easy_apply),
                -(item.estimated_steps or 99),
                item.match_score or 0,
            ),
            reverse=True,
        )

    def estimate_complexity(self, title: str, description: str) -> dict:
        text = f"{title}\n{description}".lower()
        screening_count = len(re.findall(r"\b(required|minimum|must have|question|years of|experience with)\b", text))
        free_text = len(re.findall(r"\b(cover letter|essay|explain|describe|why are you)\b", text))
        salary = bool(re.search(r"\b(salary|compensation|ctc|hourly|pay range)\b", text))
        legal = bool(re.search(r"\b(visa|sponsorship|authorized|work authorization|background check)\b", text))
        employment = bool(re.search(r"\b(employment history|work history|previous employer|start date|end date)\b", text))
        required_fields = min(screening_count + free_text + int(salary) + int(legal) + int(employment), 12)
        estimated_steps = 1 + min(required_fields // 3, 4) + int(employment)
        penalty = required_fields * 6 + free_text * 8 + int(salary) * 10 + int(legal) * 10 + int(employment) * 18
        score = max(0, min(100, 100 - penalty))
        if score >= 75:
            level = "Simple"
        elif score >= 45:
            level = "Medium"
        else:
            level = "Complex"
        return {
            "complexity_score": round(score, 2),
            "complexity_level": level,
            "estimated_steps": max(1, estimated_steps),
            "screening_question_count": min(screening_count, 20),
            "required_fields": required_fields,
            "employment_history_required": employment,
        }

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

    def duplicate_key(self, raw: dict) -> tuple[str, ...]:
        job_url = str(raw.get("job_url") or "").split("?")[0].rstrip("/")
        job_id = self.linkedin_job_id(job_url)
        title = self._normalize(raw.get("title") or "")
        company = self._normalize(raw.get("company") or "")
        location = self._normalize(raw.get("location") or "")
        return (job_url, job_id, f"{title}|{company}|{location}")

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
    ) -> None:
        query = re.sub(r"\s+", " ", (query or "").strip())
        if not query:
            return
        candidate = SearchVariation(query=query, location=location, role=role, expanded_role=expanded_role, pass_name=pass_name)
        key = (candidate.query.lower(), candidate.location.lower())
        if key not in {(item.query.lower(), item.location.lower()) for item in queries}:
            queries.append(candidate)

    def _normalize(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
