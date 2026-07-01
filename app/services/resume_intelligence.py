import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.models import CandidateProfile, JobOpportunity, ResumeProfile
from app.services.reasoning import AIReasoningEngine, CONFIDENCE_RANK


@dataclass
class ResumeIntelligenceResult:
    overall_match: float
    semantic_match: float
    technical_match: float
    experience_match: float
    education_match: float
    domain_match: float
    soft_skill_match: float
    industry_match: float
    priority_score: float
    confidence: str
    matched_skills: list[str]
    missing_skills: list[str]
    matched_technologies: list[str]
    missing_technologies: list[str]
    matched_experience: list[str]
    missing_experience: list[str]
    matched_education: list[str]
    strengths: list[str]
    weaknesses: list[str]
    risks: list[str]
    recommendations: list[str]
    overall_summary: str
    generated_answers: list[dict]
    cover_letter: str
    resume_suggestions: list[dict]
    reasoning_summary: str


class ResumeIntelligenceEngine:
    COMMON_PROMPTS = [
        "Why are you interested in this role?",
        "Tell us about yourself.",
        "Describe your SQL experience.",
        "Describe your Python experience.",
        "Describe your Power BI experience.",
        "Describe your dashboard experience.",
        "Describe your ETL experience.",
        "Describe your analytics experience.",
        "Describe your communication skills.",
        "Describe a challenging project.",
    ]

    def __init__(self, settings: Settings, reasoning: AIReasoningEngine | None = None):
        self.settings = settings
        self.reasoning = reasoning or AIReasoningEngine(settings)

    def create_debug_dir(self) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        debug_dir = self.settings.resume_intelligence_debug_dir / timestamp
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def analyze_job(
        self,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None = None,
        employment_history: list[dict] | None = None,
        candidate_knowledge: list[dict] | None = None,
        recruiter_memory: list[dict] | None = None,
    ) -> ResumeIntelligenceResult:
        metadata = job.job_description_metadata or self.reasoning.analyze_job_description(job)
        job.job_description_metadata = metadata
        comparison = self.reasoning.compare_resume_to_job(resume, job)
        matched_skills = comparison.get("matched_skills") or []
        missing_skills = comparison.get("missing_skills") or []
        matched_technologies = comparison.get("matched_technologies") or []
        missing_technologies = comparison.get("missing_technologies") or []
        matched_experience = comparison.get("matched_experience") or []
        missing_experience = comparison.get("missing_experience") or []
        matched_education = self._matched_education(resume, metadata)

        semantic = float(comparison.get("overall_semantic_match") or 0)
        technical = self._score_from_counts(matched_skills, missing_skills)
        experience = 85.0 if matched_experience else 55.0 if resume.experience else 35.0
        education = 90.0 if matched_education else 60.0 if resume.education else 35.0
        domain = self._domain_match(resume, metadata)
        soft = self._soft_skill_match(resume, metadata)
        industry = self._industry_match(resume, metadata)
        priority = float(job.priority_score or job.match_score or 0)
        overall = round(
            semantic * 0.25
            + technical * 0.25
            + experience * 0.15
            + education * 0.10
            + domain * 0.10
            + soft * 0.05
            + industry * 0.05
            + min(priority, 100) * 0.05,
            2,
        )
        strengths = self._strengths(resume, matched_skills, matched_technologies, matched_experience, matched_education)
        weaknesses = list(dict.fromkeys(missing_skills + missing_technologies))[:8]
        risks = self._risks(job, missing_skills, metadata)
        recommendations = self._recommendations(missing_skills, metadata)
        confidence = self._confidence(overall, strengths, weaknesses)
        overall_summary = self._overall_summary(job, overall, strengths, weaknesses)
        generated_answers = [
            self.generate_answer(prompt, resume, job, candidate_profile, employment_history or [], candidate_knowledge or [], recruiter_memory or [])
            for prompt in self.COMMON_PROMPTS
        ]
        cover_letter = self.generate_cover_letter(
            resume,
            job,
            candidate_profile,
            strengths,
            missing_skills,
            metadata,
        )
        suggestions = self.resume_suggestions(resume, job, missing_skills, metadata)
        reasoning_summary = (
            f"{job.title} at {job.company}: {overall:.0f}% overall fit. "
            f"Matched {len(matched_skills)} required skills and found {len(missing_skills)} missing skills."
        )
        return ResumeIntelligenceResult(
            overall,
            semantic,
            technical,
            experience,
            education,
            domain,
            soft,
            industry,
            priority,
            confidence,
            matched_skills,
            missing_skills,
            matched_technologies,
            missing_technologies,
            matched_experience,
            missing_experience,
            matched_education,
            strengths,
            weaknesses,
            risks,
            recommendations,
            overall_summary,
            generated_answers,
            cover_letter,
            suggestions,
            reasoning_summary,
        )

    def generate_answer(
        self,
        prompt: str,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None = None,
        employment_history: list[dict] | None = None,
        candidate_knowledge: list[dict] | None = None,
        recruiter_memory: list[dict] | None = None,
    ) -> dict:
        decision = self.reasoning.answer_question(
            question=prompt,
            resume=resume,
            job=job,
            candidate_profile=candidate_profile,
            employment_history=employment_history or [],
            candidate_knowledge=candidate_knowledge or [],
            recruiter_memory=recruiter_memory or [],
            mode="aggressive",
            options=[],
        )
        evidence_terms = self._answer_evidence_terms(resume, job, prompt)
        if not evidence_terms:
            return {
                "prompt": prompt,
                "answer": "I would need more candidate-specific evidence before answering this confidently.",
                "confidence": "Low",
                "grounding_score": 20,
                "evidence_used": [],
                "source_list": [],
                "reasoning_summary": "No strong supporting evidence was found, so a generic answer was avoided.",
            }
        role = job.title or resume.detected_role or "this role"
        company = job.company or "your team"
        answer = self._compose_answer(prompt, role, company, evidence_terms)
        confidence_rank = max(CONFIDENCE_RANK.get(decision.confidence, 1), 3 if len(evidence_terms) >= 2 else 2)
        confidence = {5: "Very High", 4: "High", 3: "Medium", 2: "Low", 1: "Very Low"}[min(confidence_rank, 5)]
        return {
            "prompt": prompt,
            "answer": answer,
            "confidence": confidence,
            "grounding_score": min(95, 45 + len(evidence_terms) * 12),
            "evidence_used": evidence_terms,
            "source_list": ["Resume", "Job Description", "AI Reasoning Engine"],
            "reasoning_summary": decision.reasoning_summary,
        }

    def generate_cover_letter(
        self,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        strengths: list[str],
        missing_skills: list[str],
        metadata: dict,
    ) -> str:
        name = "Candidate"
        if candidate_profile and (candidate_profile.first_name or candidate_profile.last_name):
            name = " ".join([candidate_profile.first_name or "", candidate_profile.last_name or ""]).strip()
        role = job.title or "the role"
        company = job.company or "your company"
        strengths_text = ", ".join(strengths[:4]) if strengths else "the candidate's relevant background"
        responsibilities = ", ".join((metadata.get("responsibilities") or [])[:2])
        role_context = f" The responsibilities around {responsibilities} are especially relevant." if responsibilities else ""
        return (
            f"Dear Hiring Team,\n\n"
            f"I am writing to express interest in the {role} opportunity at {company}. "
            f"{name}'s background shows evidence of {strengths_text}, which aligns with the needs of this position."
            f"{role_context}\n\n"
            f"The application is grounded in the existing resume and candidate record only. "
            f"Based on the available evidence, this profile is strongest where the role requires {strengths_text}."
            f"\n\nSincerely,\n{name}"
        )

    def resume_suggestions(self, resume: ResumeProfile, job: JobOpportunity, missing_skills: list[str], metadata: dict) -> list[dict]:
        suggestions: list[dict] = []
        for skill in missing_skills[:5]:
            suggestions.append(
                {
                    "suggestion": f"If accurate, add stronger evidence for {skill} in the resume.",
                    "why_it_helps": f"The job description appears to value {skill}, but the current resume evidence is weak or missing.",
                    "source": "Job Description Comparison",
                }
            )
        for skill in (metadata.get("required_skills") or [])[:5]:
            if skill in [item.lower() for item in resume.skills or []]:
                suggestions.append(
                    {
                        "suggestion": f"Move {skill} higher in the skills or summary section.",
                        "why_it_helps": "This makes a directly matched requirement easier for recruiters and ATS filters to see.",
                        "source": "Matched Skill",
                    }
                )
                break
        if not suggestions:
            suggestions.append(
                {
                    "suggestion": "Add measurable project outcomes where possible.",
                    "why_it_helps": "Quantified outcomes make the match explanation stronger without rewriting the resume automatically.",
                    "source": "General Resume Evidence",
                }
            )
        return suggestions[:6]

    def apply_to_job(self, job: JobOpportunity, result: ResumeIntelligenceResult) -> None:
        job.semantic_match_score = result.semantic_match
        job.technical_match_score = result.technical_match
        job.industry_match_score = result.industry_match
        job.confidence_score = result.confidence
        job.matched_skills = result.matched_skills
        job.missing_skills = result.missing_skills
        job.overall_match_summary = result.overall_summary
        job.matched_skills_summary = ", ".join(result.matched_skills)
        job.missing_skills_summary = ", ".join(result.missing_skills)
        job.strength_summary = "; ".join(result.strengths)
        job.weakness_summary = "; ".join(result.weaknesses)
        job.recommendation_summary = "; ".join(result.recommendations)
        job.generated_cover_letter = result.cover_letter
        job.resume_improvement_suggestions = result.resume_suggestions
        job.match_reason = result.overall_summary

    def write_debug(self, debug_dir: Path, payloads: list[dict]) -> None:
        files = {
            "ranking_analysis.json": payloads,
            "match_explanation.json": [
                {
                    "job": item["job"],
                    "overall_summary": item["result"]["overall_summary"],
                    "strengths": item["result"]["strengths"],
                    "weaknesses": item["result"]["weaknesses"],
                    "risks": item["result"]["risks"],
                    "recommendations": item["result"]["recommendations"],
                }
                for item in payloads
            ],
            "generated_answers.json": [
                {"job": item["job"], "answers": item["result"]["generated_answers"]}
                for item in payloads
            ],
            "cover_letter.json": [
                {"job": item["job"], "cover_letter": item["result"]["cover_letter"]}
                for item in payloads
            ],
            "resume_suggestions.json": [
                {"job": item["job"], "suggestions": item["result"]["resume_suggestions"]}
                for item in payloads
            ],
            "reasoning_summary.json": [
                {"job": item["job"], "summary": item["result"]["reasoning_summary"]}
                for item in payloads
            ],
        }
        for filename, payload in files.items():
            (debug_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _compose_answer(self, prompt: str, role: str, company: str, evidence_terms: list[str]) -> str:
        terms = ", ".join(evidence_terms[:4])
        prompt_lower = prompt.lower()
        if "interested" in prompt_lower:
            return f"I am interested in the {role} opportunity at {company} because my background in {terms} aligns with the role's focus."
        if "about yourself" in prompt_lower:
            return f"My background is centered on {terms}, and I have applied these strengths in work and project settings relevant to {role}."
        if "challenging project" in prompt_lower:
            return f"A relevant challenge from my background involved using {terms} to turn data or technical requirements into a practical outcome."
        return f"My experience with {terms} is relevant to the {role} role at {company}, based on the evidence in my resume and the job description."

    def _answer_evidence_terms(self, resume: ResumeProfile, job: JobOpportunity, prompt: str) -> list[str]:
        text = self._resume_text(resume)
        prompt_terms = [term for term in self._known_terms() if term in prompt.lower()]
        terms = prompt_terms or self._known_terms()
        found = [term for term in terms if re.search(rf"\b{re.escape(term)}\b", text)]
        if not found:
            found = [term.lower() for term in (resume.skills or resume.technologies or resume.domain_expertise or [])[:4]]
        return list(dict.fromkeys(found))[:6]

    def _known_terms(self) -> list[str]:
        return [
            "sql",
            "python",
            "power bi",
            "excel",
            "dashboard",
            "etl",
            "analytics",
            "machine learning",
            "communication",
            "stakeholder",
            "fastapi",
            "mongodb",
            "react",
        ]

    def _matched_education(self, resume: ResumeProfile, metadata: dict) -> list[str]:
        education_text = json.dumps(resume.education or [], default=str).lower()
        return [item for item in metadata.get("education_requirements") or [] if item.lower() in education_text]

    def _score_from_counts(self, matched: list[str], missing: list[str]) -> float:
        total = len(matched) + len(missing)
        return 0.0 if total == 0 else round((len(matched) / total) * 100, 2)

    def _domain_match(self, resume: ResumeProfile, metadata: dict) -> float:
        domain = (metadata.get("domain") or "").lower()
        if not domain:
            return 50.0
        resume_text = self._resume_text(resume)
        return 90.0 if domain in resume_text else 40.0

    def _soft_skill_match(self, resume: ResumeProfile, metadata: dict) -> float:
        required = metadata.get("soft_skills") or []
        if not required:
            return 50.0
        resume_text = self._resume_text(resume)
        matched = [skill for skill in required if skill in resume_text]
        return self._score_from_counts(matched, [skill for skill in required if skill not in matched])

    def _industry_match(self, resume: ResumeProfile, metadata: dict) -> float:
        industry = (metadata.get("industry") or "").lower()
        if not industry:
            return 50.0
        return 90.0 if industry in self._resume_text(resume) else 35.0

    def _strengths(
        self,
        resume: ResumeProfile,
        matched_skills: list[str],
        matched_technologies: list[str],
        matched_experience: list[str],
        matched_education: list[str],
    ) -> list[str]:
        strengths = matched_skills + matched_technologies + matched_experience + matched_education
        if not strengths:
            strengths = list(resume.skills or [])[:5] + list(resume.technologies or [])[:3]
        return list(dict.fromkeys(str(item) for item in strengths if item))[:8]

    def _risks(self, job: JobOpportunity, missing_skills: list[str], metadata: dict) -> list[str]:
        risks = []
        if missing_skills:
            risks.append(f"Missing or weak evidence for: {', '.join(missing_skills[:4])}.")
        if metadata.get("years_of_experience") and metadata["years_of_experience"] > 3:
            risks.append(f"Role may expect {metadata['years_of_experience']} years of experience.")
        if not job.description:
            risks.append("Job description text is limited, so the explanation confidence is lower.")
        return risks

    def _recommendations(self, missing_skills: list[str], metadata: dict) -> list[str]:
        if missing_skills:
            return [f"Confirm whether the candidate has {missing_skills[0]} experience and add it to the profile if true."]
        if metadata.get("required_skills"):
            return ["Strong candidate. Keep the matched skills visible near the top of the resume."]
        return ["Review the job description manually if LinkedIn provided limited details."]

    def _confidence(self, overall: float, strengths: list[str], weaknesses: list[str]) -> str:
        if overall >= 80 and len(strengths) >= 4 and len(weaknesses) <= 2:
            return "Very High"
        if overall >= 65 and strengths:
            return "High"
        if overall >= 45:
            return "Medium"
        if strengths:
            return "Low"
        return "Very Low"

    def _overall_summary(self, job: JobOpportunity, overall: float, strengths: list[str], weaknesses: list[str]) -> str:
        strength_text = ", ".join(strengths[:4]) if strengths else "limited direct evidence"
        if weaknesses:
            return f"{overall:.0f}% overall match for {job.title}. Strengths include {strength_text}; gaps include {', '.join(weaknesses[:3])}."
        return f"{overall:.0f}% overall match for {job.title}. The strongest evidence is {strength_text}."

    def _resume_text(self, resume: ResumeProfile) -> str:
        return json.dumps(
            [resume.raw_text, resume.skills, resume.technologies, resume.domain_expertise, resume.experience, resume.education],
            default=str,
        ).lower()

    def _text(self, resume: ResumeProfile, job: JobOpportunity) -> str:
        return f"{self._resume_text(resume)} {job.title} {job.company} {job.description}".lower()
