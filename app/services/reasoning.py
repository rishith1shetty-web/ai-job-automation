import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.models import CandidateProfile, JobOpportunity, ResumeProfile


CONFIDENCE_RANK = {
    "Very Low": 1,
    "Low": 2,
    "Medium": 3,
    "High": 4,
    "Very High": 5,
}


@dataclass
class ReasoningDecision:
    answer: str | None
    confidence: str
    evidence_used: list[dict]
    reasoning_summary: str
    reasoning_type: str
    semantic_match_score: float = 0
    job_description_evidence: dict | None = None
    matched_skills: list[str] | None = None
    missing_skills: list[str] | None = None

    def to_event(self, *, aggressive_default_used: bool = False) -> dict:
        return {
            "answer": self.answer,
            "selected_answer": self.answer,
            "source": "AI Reasoning Engine",
            "confidence": self.confidence,
            "reason": self.reasoning_summary,
            "reasoning_type": self.reasoning_type,
            "reasoning_summary": self.reasoning_summary,
            "semantic_match_score": self.semantic_match_score,
            "evidence": {
                "evidence_used": self.evidence_used,
                "job_description_evidence": self.job_description_evidence or {},
                "matched_skills": self.matched_skills or [],
                "missing_skills": self.missing_skills or [],
                "reasoning_type": self.reasoning_type,
            },
            "aggressive_default_used": aggressive_default_used,
        }


class AIReasoningEngine:
    SKILL_ALIASES = {
        "sql": ["sql", "database query", "queries", "relational database", "mysql", "postgresql", "sql server"],
        "python": ["python", "pandas", "numpy", "scikit-learn", "fastapi", "django", "flask"],
        "excel": ["excel", "microsoft excel", "spreadsheet", "pivot table", "vlookup"],
        "power bi": ["power bi", "powerbi", "dax", "dashboard", "reporting"],
        "tableau": ["tableau", "dashboard", "visualization"],
        "etl": ["etl", "data cleaning", "data transformation", "pipeline", "csv processing", "extract transform load", "python", "pandas", "sql"],
        "large datasets": ["large dataset", "big data", "records", "data warehouse", "sql", "analytics"],
        "dashboard development": ["dashboard", "power bi", "tableau", "reports", "kpi", "visualization"],
        "stakeholder communication": ["stakeholder", "presented", "client reporting", "management", "communicated", "insights"],
        "machine learning": ["machine learning", "ml", "tensorflow", "pytorch", "model", "predictive", "classification"],
        "e-commerce": ["e-commerce", "ecommerce", "retail", "shopify", "online marketplace"],
        "healthcare": ["healthcare", "patient", "clinical", "medical"],
        "cloud": ["aws", "azure", "gcp", "cloud", "snowflake"],
    }
    JOB_METADATA_PATTERNS = {
        "required_skills": [
            "sql",
            "python",
            "excel",
            "power bi",
            "tableau",
            "machine learning",
            "etl",
            "aws",
            "azure",
            "snowflake",
            "spark",
            "react",
            "fastapi",
        ],
        "soft_skills": ["communication", "stakeholder", "collaboration", "leadership", "presentation"],
        "cloud_platforms": ["aws", "azure", "gcp", "snowflake"],
        "programming_languages": ["python", "javascript", "java", "c++", "sql"],
        "databases": ["sql", "postgresql", "mysql", "mongodb", "snowflake"],
        "bi_tools": ["power bi", "tableau", "excel", "looker"],
        "frameworks": ["fastapi", "django", "flask", "react", "tensorflow", "pytorch"],
        "certifications": ["certified", "certification", "aws certified", "azure certified"],
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def create_debug_dir(self, job: JobOpportunity | None = None) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        if job is None:
            debug_dir = self.settings.reasoning_debug_dir / timestamp
        else:
            safe_title = re.sub(r"[^A-Za-z0-9_.-]+", "_", job.title or "job")[:80]
            debug_dir = self.settings.reasoning_debug_dir / timestamp / f"job_{job.id}_{safe_title}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def analyze_job_description(self, job: JobOpportunity) -> dict:
        text = self._normalize_text(f"{job.title}\n{job.company}\n{job.location}\n{job.description}")
        required_skills = self._matched_terms(text, self.JOB_METADATA_PATTERNS["required_skills"])
        years = [int(value) for value in re.findall(r"(\d{1,2})\+?\s+years?", text)]
        industry = self._first_present(text, ["healthcare", "finance", "retail", "e-commerce", "education", "technology"])
        domain = self._first_present(text, ["analytics", "data engineering", "business intelligence", "machine learning", "software"])
        return {
            "required_skills": required_skills,
            "preferred_skills": self._matched_terms(text, ["nice to have", "preferred", "plus"]),
            "years_of_experience": max(years) if years else None,
            "technologies": required_skills,
            "responsibilities": self._extract_responsibilities(job.description or ""),
            "industry": industry,
            "domain": domain,
            "education_requirements": self._matched_terms(text, ["bachelor", "master", "degree", "computer science", "statistics"]),
            "certifications": self._matched_terms(text, self.JOB_METADATA_PATTERNS["certifications"]),
            "soft_skills": self._matched_terms(text, self.JOB_METADATA_PATTERNS["soft_skills"]),
            "management_experience": bool(re.search(r"\b(manage|manager|lead|mentor|supervise)\b", text)),
            "cloud_platforms": self._matched_terms(text, self.JOB_METADATA_PATTERNS["cloud_platforms"]),
            "programming_languages": self._matched_terms(text, self.JOB_METADATA_PATTERNS["programming_languages"]),
            "databases": self._matched_terms(text, self.JOB_METADATA_PATTERNS["databases"]),
            "bi_tools": self._matched_terms(text, self.JOB_METADATA_PATTERNS["bi_tools"]),
            "frameworks": self._matched_terms(text, self.JOB_METADATA_PATTERNS["frameworks"]),
        }

    def compare_resume_to_job(self, resume: ResumeProfile, job: JobOpportunity) -> dict:
        metadata = job.job_description_metadata or self.analyze_job_description(job)
        candidate_text = self._candidate_text(resume, None, [], [], [])
        required = list(dict.fromkeys(metadata.get("required_skills") or []))
        matched = [skill for skill in required if self._concept_present(skill, candidate_text)]
        missing = [skill for skill in required if skill not in matched]
        score = 0.0 if not required else round((len(matched) / len(required)) * 100, 2)
        return {
            "matched_skills": matched,
            "missing_skills": missing,
            "matched_technologies": matched,
            "missing_technologies": missing,
            "matched_experience": self._matched_experience(resume, metadata),
            "missing_experience": [],
            "overall_semantic_match": score,
            "job_description_metadata": metadata,
        }

    def answer_question(
        self,
        *,
        question: str,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None = None,
        employment_history: list[dict] | None = None,
        candidate_knowledge: list[dict] | None = None,
        recruiter_memory: list[dict] | None = None,
        mode: str = "safe",
        options: list[str] | None = None,
    ) -> ReasoningDecision:
        options = options or []
        metadata = job.job_description_metadata or self.analyze_job_description(job)
        comparison = self.compare_resume_to_job(resume, job)
        candidate_text = self._candidate_text(
            resume,
            candidate_profile,
            employment_history or [],
            candidate_knowledge or [],
            recruiter_memory or [],
        )
        question_concepts = self._question_concepts(question)
        evidence = self._collect_evidence(
            question_concepts,
            resume,
            candidate_profile,
            employment_history or [],
            candidate_knowledge or [],
            recruiter_memory or [],
            job,
        )
        direct_sources = [item for item in evidence if item["reasoning_type"] in {"Knowledge Base Match", "Recruiter Memory Match", "Employment Match", "Project Match", "Direct Resume Match"}]
        answer = self._answer_from_evidence(question, candidate_text, evidence, options, mode)
        confidence = self._confidence_for(evidence, answer, mode)
        reasoning_type = direct_sources[0]["reasoning_type"] if direct_sources else "Semantic Inference" if evidence else "Estimated"
        summary = self._summary(question_concepts, answer, evidence, mode)
        return ReasoningDecision(
            answer=answer,
            confidence=confidence,
            evidence_used=evidence,
            reasoning_summary=summary,
            reasoning_type=reasoning_type,
            semantic_match_score=float(comparison["overall_semantic_match"]),
            job_description_evidence=metadata,
            matched_skills=comparison["matched_skills"],
            missing_skills=comparison["missing_skills"],
        )

    def select_option(
        self,
        *,
        options: list[str],
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None = None,
        employment_history: list[dict] | None = None,
        candidate_knowledge: list[dict] | None = None,
        recruiter_memory: list[dict] | None = None,
    ) -> dict | None:
        """Choose the dropdown/select option that candidate evidence supports.

        Safe and non-fabricating: returns the first option for which concrete
        evidence exists (with confidence + reasoning), else ``None``. Never used
        for legal/authorization questions (the caller gates those out).
        """
        usable = [option for option in (options or []) if str(option).strip()]
        if not usable:
            return None
        for option in usable:
            evidence = self._collect_evidence(
                [self._normalize_text(option)],
                resume,
                candidate_profile,
                employment_history or [],
                candidate_knowledge or [],
                recruiter_memory or [],
                job,
            )
            if evidence:
                sources = ", ".join(dict.fromkeys(item["source"] for item in evidence))
                return {
                    "option": option,
                    "confidence": self._confidence_for(evidence, option, "safe"),
                    "reason": f"Evidence for '{option}' was found in {sources}.",
                    "evidence": {"evidence_used": evidence, "matched_option": option},
                }
        return None

    def write_debug(self, debug_dir: Path, job: JobOpportunity, decisions: list[ReasoningDecision]) -> None:
        comparison = self.compare_resume_to_job(job.resume, job) if job.resume else {}
        files = {
            "job_description_analysis.json": job.job_description_metadata or self.analyze_job_description(job),
            "semantic_matches.json": comparison,
            "reasoning_steps.json": [asdict(decision) for decision in decisions],
            "confidence_scores.json": [
                {
                    "answer": decision.answer,
                    "confidence": decision.confidence,
                    "reasoning_type": decision.reasoning_type,
                }
                for decision in decisions
            ],
            "evidence_used.json": [decision.evidence_used for decision in decisions],
            "question_reasoning.json": [asdict(decision) for decision in decisions],
        }
        for filename, payload in files.items():
            (debug_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _collect_evidence(
        self,
        concepts: list[str],
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        employment_history: list[dict],
        candidate_knowledge: list[dict],
        recruiter_memory: list[dict],
        job: JobOpportunity,
    ) -> list[dict]:
        sources = [
            ("Knowledge Base Match", candidate_knowledge),
            ("Recruiter Memory Match", recruiter_memory),
            ("Employment Match", employment_history or resume.experience or []),
            ("Project Match", self._project_lines(resume.raw_text or "")),
            ("Direct Resume Match", [resume.raw_text, resume.skills, resume.technologies, resume.domain_expertise, resume.education]),
            ("Education Match", resume.education or []),
            ("LinkedIn Match", [candidate_profile.professional_headline, candidate_profile.current_job_title, candidate_profile.current_company] if candidate_profile else []),
            ("Job Description Match", [job.description or "", job.title or ""]),
        ]
        evidence: list[dict] = []
        for reasoning_type, values in sources:
            text = self._normalize_text(json.dumps(values, default=str))
            for concept in concepts:
                if self._concept_present(concept, text):
                    evidence.append(
                        {
                            "source": reasoning_type.replace(" Match", ""),
                            "reasoning_type": reasoning_type,
                            "concept": concept,
                            "snippet": self._snippet(text, concept),
                        }
                    )
                    break
        return evidence

    def _answer_from_evidence(self, question: str, candidate_text: str, evidence: list[dict], options: list[str], mode: str) -> str | None:
        yes_no = self._looks_yes_no_options(options) or re.search(r"\b(do you|have you|are you|can you)\b", question, re.I)
        if yes_no:
            if evidence:
                return "Yes"
            return "No" if mode == "aggressive" else None
        if evidence and options:
            for option in options:
                if self._concept_present(option, candidate_text):
                    return option
        return None

    def _confidence_for(self, evidence: list[dict], answer: str | None, mode: str) -> str:
        if not answer:
            return "Very Low"
        if len(evidence) >= 3:
            return "Very High"
        if len(evidence) == 2:
            return "High"
        if len(evidence) == 1:
            return "Medium"
        return "Low" if mode == "aggressive" else "Very Low"

    def _summary(self, concepts: list[str], answer: str | None, evidence: list[dict], mode: str) -> str:
        if evidence:
            concepts_text = ", ".join(concepts[:4]) or "the question"
            sources = ", ".join(dict.fromkeys(item["source"] for item in evidence))
            return f"Answered {answer} because evidence for {concepts_text} was found in {sources}."
        if mode == "aggressive" and answer == "No":
            return "No supporting evidence was found across candidate sources; Aggressive Mode selected No."
        return "No reliable evidence was found across candidate sources."

    # Non-discriminative words that must never count as standalone evidence
    # concepts (otherwise generic questions like "Do you have X experience?"
    # falsely match the word "experience" in any resume).
    GENERIC_CONCEPT_WORDS = {
        "experience", "experiences", "work", "working", "role", "roles", "job",
        "jobs", "position", "positions", "years", "year", "background", "skill",
        "skills", "knowledge", "expertise", "field", "related", "relevant",
        "professional", "total", "overall", "level", "do you have", "have you",
        "are you", "can you", "with", "the following", "hands on", "hands-on",
    }

    def _question_concepts(self, question: str) -> list[str]:
        text = self._normalize_text(question)
        concepts = [concept for concept in self.SKILL_ALIASES if self._concept_present(concept, text)]
        for phrase in re.findall(r"\b[A-Za-z][A-Za-z0-9+#. ]{2,30}\b", question):
            lowered = self._normalize_text(phrase)
            # Drop generic filler words from the phrase so only meaningful terms
            # remain; skip the phrase entirely if nothing meaningful is left.
            meaningful = " ".join(
                word for word in lowered.split() if word not in self.GENERIC_CONCEPT_WORDS
            ).strip()
            if not meaningful or meaningful in self.GENERIC_CONCEPT_WORDS:
                continue
            concepts.append(meaningful)
        return list(dict.fromkeys(concepts))[:8]

    def _candidate_text(
        self,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        employment_history: list[dict],
        candidate_knowledge: list[dict],
        recruiter_memory: list[dict],
    ) -> str:
        profile = []
        if candidate_profile:
            profile = [
                candidate_profile.current_job_title,
                candidate_profile.professional_headline,
                candidate_profile.current_company,
                candidate_profile.current_location,
                candidate_profile.previous_companies,
                candidate_profile.certifications,
            ]
        return self._normalize_text(
            json.dumps(
                [
                    resume.raw_text,
                    resume.skills,
                    resume.technologies,
                    resume.domain_expertise,
                    resume.keywords,
                    resume.experience,
                    resume.education,
                    profile,
                    employment_history,
                    candidate_knowledge,
                    recruiter_memory,
                ],
                default=str,
            )
        )

    def _concept_present(self, concept: str, text: str) -> bool:
        normalized = self._normalize_text(text)
        concept_normalized = self._normalize_text(concept)
        terms = self.SKILL_ALIASES.get(concept_normalized, [concept_normalized])
        return any(re.search(rf"\b{re.escape(self._normalize_text(term))}\b", normalized) for term in terms if term)

    def _matched_terms(self, text: str, terms: list[str]) -> list[str]:
        return [term for term in terms if self._concept_present(term, text)]

    def _matched_experience(self, resume: ResumeProfile, metadata: dict) -> list[str]:
        candidate_text = self._normalize_text(json.dumps(resume.experience or [], default=str))
        return [skill for skill in metadata.get("required_skills") or [] if self._concept_present(skill, candidate_text)]

    def _first_present(self, text: str, terms: list[str]) -> str | None:
        for term in terms:
            if self._concept_present(term, text):
                return term.title()
        return None

    def _extract_responsibilities(self, description: str) -> list[str]:
        lines = [re.sub(r"\s+", " ", line).strip(" -•\t") for line in (description or "").splitlines()]
        return [line for line in lines if len(line) > 25][:8]

    def _project_lines(self, text: str) -> list[str]:
        return [line for line in text.splitlines() if re.search(r"project|built|developed|created|implemented", line, re.I)]

    def _snippet(self, text: str, concept: str) -> str:
        terms = self.SKILL_ALIASES.get(self._normalize_text(concept), [concept])
        for term in terms:
            match = re.search(re.escape(self._normalize_text(term)), text)
            if match:
                start = max(0, match.start() - 80)
                end = min(len(text), match.end() + 80)
                return text[start:end]
        return ""

    def _looks_yes_no_options(self, options: list[str]) -> bool:
        lowered = {option.lower() for option in options}
        return bool({"yes", "no"} & lowered)

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip()
