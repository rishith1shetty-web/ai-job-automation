from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings
from app.models import CandidateProfile, JobOpportunity, ResumeProfile
from app.services.reasoning import AIReasoningEngine


# Categories that must NEVER be fabricated (visa / work authorization /
# sponsorship / security clearance / criminal-background-legal). They are
# answerable only from an explicit saved answer (Screening Knowledge Base) or the
# Candidate Profile; otherwise the engine returns ``None`` (-> needs_review).
# Everything non-legal (skills, years, education, work preference, dropdowns,
# generic yes/no) is auto-answered from resume/profile evidence, and when the
# answer is not present it is AI-generated conservatively.
SAFE_STOP_CATEGORIES = {"authorization", "legal", "clearance"}

# Options that carry no evidentiary value when matching dropdown/select answers.
NON_EVIDENCE_OPTIONS = {
    "", "select", "select one", "please select", "choose", "choose one",
    "n/a", "na", "none", "other", "prefer not to say", "--",
}


@dataclass
class ScreeningQuestion:
    """Portal-agnostic, structured description of one screening question."""

    text: str
    category: str | None = None
    field_type: str | None = None   # DOM control type: radio/checkbox/text/select
    field_kind: str | None = None   # field_state["field_type"]: select/text/textarea/input
    options: list[str] = field(default_factory=list)
    normalized_question: str | None = None
    portal: str = "generic"


@dataclass
class ScreeningDecision:
    """Structured decision output. ``event`` carries the exact legacy dict shape
    that portal automations already consume, so behaviour is unchanged."""

    answer: str | None
    needs_review: bool
    category: str
    confidence: str
    source: str | None
    reasoning: str | None
    decision_stage: str | None
    currency: str | None
    evidence: dict
    aggressive_default_used: bool
    reasoning_used: bool
    decision_pipeline: list
    matched_profile_field: str | None
    matched_resume_evidence: dict
    event: dict | None

    @classmethod
    def from_event(cls, event: dict | None, category: str) -> "ScreeningDecision":
        if not event:
            return cls(
                answer=None, needs_review=True, category=category, confidence="None",
                source=None, reasoning=None, decision_stage=None, currency=None,
                evidence={}, aggressive_default_used=False, reasoning_used=False,
                decision_pipeline=[], matched_profile_field=None, matched_resume_evidence={},
                event=None,
            )
        evidence = event.get("evidence") or {}
        return cls(
            answer=event.get("selected_answer", event.get("answer")),
            needs_review=False,
            category=category,
            confidence=event.get("confidence") or "Unknown",
            source=event.get("source"),
            reasoning=event.get("reason"),
            decision_stage=event.get("decision_stage"),
            currency=event.get("currency"),
            evidence=evidence,
            aggressive_default_used=bool(event.get("aggressive_default_used")),
            reasoning_used=bool(event.get("reasoning_used")),
            decision_pipeline=event.get("decision_pipeline") or [],
            matched_profile_field=(evidence.get("source") if isinstance(evidence, dict) else None),
            matched_resume_evidence=(evidence if isinstance(evidence, dict) else {}),
            event=event,
        )

    def to_event(self) -> dict | None:
        """Return the legacy event dict (or None for needs_review)."""
        return self.event

    def to_metadata(self) -> dict:
        return {
            "answer": self.answer,
            "needs_review": self.needs_review,
            "category": self.category,
            "confidence": self.confidence,
            "answer_source": self.source,
            "reasoning": self.reasoning,
            "decision_stage": self.decision_stage,
            "matched_profile_field": self.matched_profile_field,
            "matched_resume_evidence": self.matched_resume_evidence,
            "decision_pipeline": self.decision_pipeline,
        }


class ScreeningAnswerEngine:
    """Portal-agnostic screening answer decision engine.

    Independent of Playwright, DOM, and any portal selectors. It operates only on
    structured data (question text, options, resume, candidate profile, employment
    history, saved/learned answers) and returns a :class:`ScreeningDecision`.
    The decision pipeline is preserved exactly:

        saved -> learned -> candidate profile/evidence -> safe legal stop
        -> reasoning -> estimator -> fallback -> needs_review
    """

    def __init__(self, settings: Settings, reasoning: AIReasoningEngine | None = None) -> None:
        self.settings = settings
        self.reasoning = reasoning or AIReasoningEngine(settings)

    # ---- Public interface (used by LinkedIn today, Dice/other portals later) ----
    def decide(
        self,
        question: ScreeningQuestion,
        *,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        context: dict | None,
    ) -> ScreeningDecision:
        category = question.category or self._screening_question_category(question.text)
        field_state = {"type": question.field_type, "field_type": question.field_kind}
        event = self._select_screening_answer(
            question.text,
            category,
            field_state,
            list(question.options or []),
            resume,
            job,
            candidate_profile,
            context or {},
        )
        return ScreeningDecision.from_event(event, category=category)

    def categorize(self, question: str) -> str:
        return self._screening_question_category(question)

    def matching_saved_answer(self, question: str, category: str, answers: list[dict]) -> dict | None:
        return self._matching_saved_screening_answer(question, category, answers)

    def normalize_question(self, question: str) -> str:
        return self._normalize_screening_question(question)

    def extract_skill_from_experience_question(self, question: str) -> str | None:
        return self._extract_skill_from_experience_question(question)

    def estimate_skill_years(self, skill: str, resume: ResumeProfile, candidate_profile: CandidateProfile | None) -> dict:
        return self._estimate_skill_years(skill, resume, candidate_profile)

    def extract_location_from_resume(self, resume: ResumeProfile) -> str | None:
        return self._extract_location_from_resume(resume)

    # ===== Moved verbatim from LinkedInAutomation (unchanged decision logic) =====
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
        mode = (screening_context.get("mode") or "safe").lower()
        pipeline: list[dict] = []
        saved = self._screening_decision_from_saved(question, category, screening_context)
        self._record_screening_pipeline_stage(pipeline, "saved_answer", saved)
        if saved:
            return self._finalize_screening_decision(saved, "saved_answer", pipeline)

        stages = [
            (
                "learned_mapping",
                lambda: self._screening_decision_from_learned_mapping(question, category, field_state, options, screening_context),
            ),
            (
                "evidence",
                lambda: self._screening_decision_from_evidence(question, category, resume, options, candidate_profile),
            ),
        ]
        for stage_name, stage in stages:
            candidate = stage()
            self._record_screening_pipeline_stage(pipeline, stage_name, candidate)
            if candidate:
                return self._finalize_screening_decision(candidate, stage_name, pipeline)
        if category in SAFE_STOP_CATEGORIES:
            return None
        remaining_stages = [
            (
                "reasoning",
                lambda: self._screening_decision_from_reasoning(
                    question,
                    category,
                    options,
                    resume,
                    job,
                    candidate_profile,
                    screening_context,
                    mode,
                ),
            ),
            (
                "estimator",
                lambda: self._screening_decision_from_estimator(
                    question,
                    category,
                    resume,
                    job,
                    candidate_profile,
                    screening_context,
                    mode,
                ),
            ),
            (
                "fallback",
                lambda: self._screening_decision_from_fallback(
                    question,
                    category,
                    field_state,
                    options,
                    resume,
                    job,
                    mode,
                ),
            ),
            (
                "coverage",
                lambda: self._screening_decision_from_coverage(
                    question,
                    category,
                    field_state,
                    options,
                    resume,
                    job,
                    candidate_profile,
                    screening_context,
                    mode,
                ),
            ),
        ]
        for stage_name, stage in remaining_stages:
            candidate = stage()
            self._record_screening_pipeline_stage(pipeline, stage_name, candidate)
            if candidate:
                return self._finalize_screening_decision(candidate, stage_name, pipeline)
        return None

    def _screening_decision_from_saved(
        self,
        question: str,
        category: str,
        screening_context: dict,
    ) -> dict | None:
        saved = self._matching_saved_screening_answer(question, category, screening_context.get("saved_answers") or [])
        if not saved:
            return None
        return {
            "answer": saved["answer"],
            "selected_answer": saved["answer"],
            "source": saved.get("source") or "Candidate Knowledge Base",
            "confidence": saved.get("confidence") or "High",
            "currency": saved.get("currency"),
            "reason": "Reused saved candidate-specific screening answer.",
            "evidence": saved.get("evidence") or {},
            "aggressive_default_used": False,
        }

    def _screening_decision_from_learned_mapping(
        self,
        question: str,
        category: str,
        field_state: dict,
        options: list[str],
        screening_context: dict,
    ) -> dict | None:
        return None

    def _screening_decision_from_evidence(
        self,
        question: str,
        category: str,
        resume: ResumeProfile,
        options: list[str] | None = None,
        candidate_profile: CandidateProfile | None = None,
    ) -> dict | None:
        options = options or []
        explicit_profile_answer = self._explicit_candidate_profile_answer(question, category, candidate_profile, options)
        if explicit_profile_answer:
            return explicit_profile_answer
        if category == "graduation_year":
            year = self._extract_graduation_year(resume)
            if not year:
                return None
            return self._screening_event(
                str(year),
                "Resume Education",
                "High",
                "Graduation year was found in resume education text.",
                False,
                evidence={"graduation_year": year, "source": "resume.education/raw_text"},
            )
        if category == "education" and options and not self._looks_yes_no_options(options):
            degree_option = self._degree_option_from_resume(resume, options)
            if degree_option:
                return self._screening_event(
                    degree_option,
                    "Resume Education",
                    "High",
                    "Degree option was selected from resume education evidence.",
                    False,
                    evidence={"degree_option": degree_option, "education": resume.education or [], "raw_text_match": resume.raw_text},
                )
        if category != "salary":
            return None
        explicit = self._extract_salary_from_text(resume.raw_text)
        if not explicit:
            return None
        return self._screening_event(
            explicit["value"],
            "Resume",
            "High",
            "Salary value was explicitly mentioned in the resume.",
            False,
            currency=explicit.get("currency"),
            evidence=explicit,
        )

    def _explicit_candidate_profile_answer(
        self,
        question: str,
        category: str,
        candidate_profile: CandidateProfile | None,
        options: list[str],
    ) -> dict | None:
        if candidate_profile is None:
            return None
        question_text = question.lower()
        yes_no = self._looks_yes_no_options(options) or re.search(r"\b(do you|have you|are you|can you)\b", question_text)
        profile_values = [
            candidate_profile.certifications or [],
            candidate_profile.professional_links or {},
            candidate_profile.field_sources or {},
        ]
        profile_text = json.dumps(profile_values, default=str).lower()
        if category == "clearance" and "clearance" in profile_text and yes_no:
            return self._screening_event(
                "Yes",
                "Candidate Profile",
                "High",
                "Candidate Profile explicitly lists security clearance evidence.",
                False,
                evidence={"source": "candidate_profile.certifications", "matched": "clearance"},
            )
        if category == "certification" and yes_no:
            if re.search(r"driver'?s?\s+licen[cs]e|driving\s+licen[cs]e", question_text) and re.search(
                r"driver'?s?\s+licen[cs]e|driving\s+licen[cs]e",
                profile_text,
            ):
                return self._screening_event(
                    "Yes",
                    "Candidate Profile",
                    "High",
                    "Candidate Profile explicitly lists a driver's license.",
                    False,
                    evidence={"source": "candidate_profile.certifications", "matched": "driver_license"},
                )
            certificate_terms = [
                term
                for term in re.findall(r"[a-z][a-z0-9+#.]{2,}", question_text)
                if term not in {"have", "you", "certification", "certified", "license", "valid", "following"}
            ]
            if certificate_terms and any(term in profile_text for term in certificate_terms):
                return self._screening_event(
                    "Yes",
                    "Candidate Profile",
                    "High",
                    "Candidate Profile certifications explicitly match the requested credential.",
                    False,
                    evidence={"source": "candidate_profile.certifications", "matched_terms": certificate_terms},
                )
        return None

    def _screening_decision_from_reasoning(
        self,
        question: str,
        category: str,
        options: list[str],
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
        mode: str,
    ) -> dict | None:
        if category not in {"skill", "education", "yes_no"} and not self._looks_yes_no_options(options):
            return None
        reasoning = self.reasoning.answer_question(
            question=question,
            resume=resume,
            job=job,
            candidate_profile=candidate_profile,
            employment_history=screening_context.get("employment_history") or [],
            candidate_knowledge=screening_context.get("saved_answers") or [],
            recruiter_memory=screening_context.get("recruiter_memory") or [],
            mode=mode,
            options=options,
        )
        if not reasoning.answer or not reasoning.evidence_used:
            return None
        event = reasoning.to_event(aggressive_default_used=False)
        event["reasoning_used"] = True
        return event

    def _screening_decision_from_estimator(
        self,
        question: str,
        category: str,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
        mode: str,
    ) -> dict | None:
        if category == "salary":
            if mode != "aggressive":
                return None
            estimate = self._estimate_salary(question, resume, job, candidate_profile)
            if not estimate:
                return None
            return self._screening_event(
                estimate["answer"],
                "AI Estimated Salary",
                "Estimated / Low",
                "No explicit salary found. Value estimated from resume, role, location, experience, and job context.",
                True,
                currency=estimate["currency"],
                evidence=estimate,
            )
        if category == "skill_years":
            return self._skill_experience_screening_answer(
                question,
                resume,
                candidate_profile,
                screening_context,
                mode,
            )
        # Work-preference willingness (relocation / commute / remote / hybrid) is
        # non-legal; answer it with the application-friendly conservative default in
        # both safe and aggressive modes (before the aggressive-only defaults below).
        if category == "work_preference":
            return self._screening_event(
                "Yes",
                "Conservative Default",
                "Estimated / Low",
                "Non-legal work-preference question; defaulted to willing.",
                True,
                evidence={"preference": "willing"},
            )
        if mode != "aggressive":
            return None
        if category == "notice":
            value = screening_context.get("notice_period_default") or "30 days"
            return self._screening_event(value, "Aggressive Default", "Estimated / Low", "Used configured notice period default.", True)
        if category == "location":
            location = (
                (candidate_profile.current_location if candidate_profile else None)
                or self._extract_location_from_resume(resume)
                or job.location
            )
            if location:
                return self._screening_event(location, "Candidate Profile", "Medium", "Used candidate or job location.", False)
            return None
        if category == "years":
            years = self._estimated_years_of_experience(resume)
            return self._screening_event(str(years), "Aggressive Default", "Estimated / Low", "Years were not explicit enough; used conservative default.", True)
        return None

    def _screening_decision_from_fallback(
        self,
        question: str,
        category: str,
        field_state: dict,
        options: list[str],
        resume: ResumeProfile,
        job: JobOpportunity,
        mode: str,
    ) -> dict | None:
        if mode != "aggressive":
            return None
        yes_no_control = self._looks_yes_no_options(options) or str(field_state.get("type") or "").lower() in {"radio", "checkbox"}
        if (category in {"skill", "education", "yes_no"} and yes_no_control) or self._looks_yes_no_options(options):
            yes_supported = self._question_supported_by_resume(question, resume)
            answer = "Yes" if yes_supported else "No"
            source = "Resume Evidence" if yes_supported else "Aggressive Default"
            confidence = "Medium" if yes_supported else "Estimated / Low"
            reason = "Resume/profile evidence supports Yes." if yes_supported else "No supporting evidence found; Aggressive Mode selected No."
            return self._screening_event(answer, source, confidence, reason, not yes_supported)
        if category not in {"yes_no", "education", "graduation_year", "authorization", "legal", "clearance", "certification"} and field_state.get("field_type") in {"text", "textarea", "input"}:
            generated = self._generated_short_screening_answer(question, resume, job)
            if generated:
                return self._screening_event(generated, "AI Generated", "Estimated / Low", "Generated concise answer from resume and job context.", True)
        return None

    def _screening_decision_from_coverage(
        self,
        question: str,
        category: str,
        field_state: dict,
        options: list[str],
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
        mode: str,
    ) -> dict | None:
        """Evidence-based coverage for structured fields that the earlier stages
        leave unanswered (notably in ``safe`` mode).

        This layer NEVER fabricates: it only answers when concrete resume/profile
        evidence exists. Legal categories are already short-circuited before this
        runs, and free-text/essay fields are deliberately left to needs_review to
        preserve truthful, human-reviewed content.
        """
        if category in SAFE_STOP_CATEGORIES:
            return None

        yes_no = self._looks_yes_no_options(options)

        # (a0) Skill-years numeric field with no resume evidence -> honest 0 years.
        # (The estimator already returns a positive number when evidence exists;
        # coverage only runs after it, so reaching here means no evidence.)
        if category == "skill_years":
            skill = self._extract_skill_from_experience_question(question)
            if skill:
                estimate = self._estimate_skill_years(skill, resume, candidate_profile)
                if estimate.get("found") and estimate.get("years"):
                    years = max(1, int(estimate["years"]))
                    return self._screening_event(
                        str(years),
                        estimate.get("source") or "Resume",
                        "Estimated",
                        estimate.get("reason") or "Estimated years for a skill present in the resume.",
                        bool(estimate.get("aggressive_default_used")),
                        evidence=estimate,
                    )
            return self._screening_event(
                "0",
                "AI Generated",
                "Estimated / Low",
                f"No resume evidence for {skill or 'this skill'}; entered 0 years so the numeric field is valid.",
                True,
                evidence={"normalized_skill": skill, "years": 0},
            )

        # (a) Numeric years-of-experience: fill from the resume, else honest 0.
        if category == "years":
            years = self._estimated_years_of_experience(resume)
            source = "Resume" if years > 0 else "AI Generated"
            confidence = "Medium" if years > 0 else "Estimated / Low"
            reason = (
                "Years of experience were stated explicitly in the resume."
                if years > 0
                else "No explicit years in the resume; entered a conservative estimate."
            )
            return self._screening_event(
                str(years), source, confidence, reason, years == 0,
                evidence={"years": years, "source": "resume.raw_text"},
            )

        # (a2) Education yes/no: answer from the resume's highest degree level.
        if category == "education" and yes_no:
            answer = self._education_yes_no_answer(question, resume)
            return self._screening_event(
                answer,
                "Resume Education",
                "Medium",
                "Compared the requested degree level with the resume's highest degree.",
                answer == "No",
                evidence={"education": resume.education or []},
            )

        # (a3) Work-preference willingness (relocation / commute / remote / hybrid):
        # non-legal; answered with the application-friendly conservative default.
        if category == "work_preference" and (yes_no or not options):
            return self._screening_event(
                "Yes",
                "Conservative Default",
                "Estimated / Low",
                "Non-legal work-preference question; defaulted to willing.",
                True,
                evidence={"preference": "willing"},
            )

        # (b) Location from candidate profile, resume, or the job itself.
        if category == "location":
            profile_location = candidate_profile.current_location if candidate_profile else None
            location = profile_location or self._extract_location_from_resume(resume) or job.location
            if location:
                source = "Candidate Profile" if profile_location else "Resume/Job Location"
                return self._screening_event(
                    location,
                    source,
                    "Medium",
                    "Answered from the candidate/job location on record.",
                    False,
                    evidence={"location": location, "source": source},
                )
            return None

        # (c) Dropdown / select / multi-option with a concrete match in the resume
        #     or profile (skip Yes/No; those are handled by evidence/reasoning).
        if options and not yes_no:
            matched = self._best_option_from_evidence(options, resume, job, candidate_profile)
            if matched:
                return self._screening_event(
                    matched["option"],
                    matched["source"],
                    matched.get("confidence") or "Medium",
                    matched["reason"],
                    False,
                    evidence=matched.get("evidence") or {},
                )
            return None

        # (d) Generic non-legal yes/no: answer from resume evidence, else AI-generate
        # the honest conservative answer ("No" when nothing supports a "Yes").
        if yes_no or str(field_state.get("type") or "").lower() in {"radio", "checkbox"}:
            supported = self._question_supported_by_resume(question, resume)
            answer = "Yes" if supported else "No"
            source = "Resume Evidence" if supported else "AI Generated"
            confidence = "Medium" if supported else "Estimated / Low"
            reason = (
                "Resume/profile evidence supports Yes."
                if supported
                else "No supporting evidence found; AI-generated the conservative answer (No)."
            )
            return self._screening_event(answer, source, confidence, reason, not supported)

        return None

    def _education_yes_no_answer(self, question: str, resume: ResumeProfile) -> str:
        """Answer a 'Have you completed <level> degree?' question by comparing the
        requested level with the resume's highest degree."""
        levels = [
            (r"doctor|phd|ph\.d|doctorate", 5),
            (r"master|m\.sc|msc|m\.?tech|mba|post\s*graduate|graduate degree", 4),
            (r"bachelor|b\.e\b|b\.?tech|b\.sc|bsc|under\s*graduate|engineering degree", 3),
            (r"associate|diploma", 2),
            (r"high school|secondary|hsc|ssc|12th|10th", 1),
        ]
        text = (json.dumps(resume.education or [], default=str) + " " + (resume.raw_text or "")).lower()
        have = 0
        for pattern, rank in levels:
            if re.search(pattern, text):
                have = max(have, rank)
        question_lower = question.lower()
        asked = None
        for pattern, rank in levels:
            if re.search(pattern, question_lower):
                asked = rank
                break
        if asked is None:
            return "Yes" if have >= 2 else "No"
        return "Yes" if have >= asked else "No"

    def _best_option_from_evidence(
        self,
        options: list[str],
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
    ) -> dict | None:
        """Pick the dropdown/select option that is directly supported by the
        candidate's resume or profile. Prefers the reasoning engine's evidence
        machinery, then falls back to a token-presence heuristic. Returns ``None``
        when nothing is defensibly supported (-> needs_review)."""
        usable = [
            option
            for option in options
            if str(option).strip().lower() not in NON_EVIDENCE_OPTIONS
        ]
        if not usable:
            return None

        # Reasoning-engine option selection (confidence + reasoning).
        selection = self.reasoning.select_option(
            options=usable,
            resume=resume,
            job=job,
            candidate_profile=candidate_profile,
        )
        if selection and selection.get("option"):
            return {
                "option": selection["option"],
                "source": "AI Reasoning Engine",
                "confidence": selection.get("confidence") or "Medium",
                "reason": selection.get("reason")
                or "Selected the option supported by resume/profile evidence.",
                "evidence": selection.get("evidence") or {},
            }

        # Heuristic fallback: option text appears verbatim in candidate evidence.
        evidence_text = " ".join(
            [
                resume.raw_text or "",
                " ".join(resume.skills or []),
                " ".join(resume.technologies or []),
                " ".join(resume.keywords or []),
                " ".join(resume.domain_expertise or []),
                (candidate_profile.professional_headline if candidate_profile else "") or "",
                (candidate_profile.current_job_title if candidate_profile else "") or "",
            ]
        ).lower()
        for option in usable:
            token = str(option).strip().lower()
            if len(token) >= 3 and re.search(rf"\b{re.escape(token)}\b", evidence_text):
                return {
                    "option": option,
                    "source": "Resume Evidence",
                    "confidence": "Medium",
                    "reason": f"Option '{option}' appears directly in the resume/profile.",
                    "evidence": {"matched_option": option},
                }
        return None

    def _record_screening_pipeline_stage(self, pipeline: list[dict], stage_name: str, candidate: dict | None) -> None:
        pipeline.append(
            {
                "stage": stage_name,
                "candidate_answer": candidate.get("selected_answer") if candidate else None,
                "source": candidate.get("source") if candidate else None,
                "confidence": candidate.get("confidence") if candidate else None,
                "evidence_used": bool((candidate or {}).get("evidence")),
                "reasoning_summary": candidate.get("reason") if candidate else None,
                "selected": bool(candidate),
            }
        )

    def _finalize_screening_decision(self, candidate: dict, stage_name: str, pipeline: list[dict]) -> dict:
        candidate.setdefault("evidence", {})
        candidate.setdefault("source", stage_name)
        candidate.setdefault("confidence", "Unknown")
        candidate.setdefault("reason", "")
        candidate["decision_stage"] = stage_name
        candidate["decision_pipeline"] = pipeline
        return candidate

    def _salary_screening_answer(
        self,
        question: str,
        field_state: dict,
        options: list[str],
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
        mode: str,
    ) -> dict | None:
        explicit = self._extract_salary_from_text(resume.raw_text)
        if explicit:
            return self._screening_event(
                explicit["value"],
                "Resume",
                "High",
                "Salary value was explicitly mentioned in the resume.",
                False,
                currency=explicit.get("currency"),
                evidence=explicit,
            )
        if mode != "aggressive":
            return None
        estimate = self._estimate_salary(question, resume, job, candidate_profile)
        if not estimate:
            return None
        return self._screening_event(
            estimate["answer"],
            "AI Estimated Salary",
            "Estimated / Low",
            "No explicit salary found. Value estimated from resume, role, location, experience, and job context.",
            True,
            currency=estimate["currency"],
            evidence=estimate,
        )

    def _screening_event(
        self,
        answer: str,
        source: str,
        confidence: str,
        reason: str,
        aggressive_default_used: bool,
        *,
        currency: str | None = None,
        evidence: dict | None = None,
    ) -> dict:
        return {
            "answer": str(answer),
            "selected_answer": str(answer),
            "source": source,
            "confidence": confidence,
            "currency": currency,
            "reason": reason,
            "evidence": evidence or {},
            "aggressive_default_used": aggressive_default_used,
        }

    def _screening_question_category(self, question: str) -> str:
        text = question.lower()
        if self._extract_skill_from_experience_question(question):
            return "skill_years"
        if re.search(r"graduation\s+year|year\s+of\s+graduation|completed\s+degree.*year|degree.*completed.*year", text):
            return "graduation_year"
        if re.search(r"salary|compensation|hourly|ctc|pay rate|rate", text):
            return "salary"
        if re.search(r"visa|sponsor|authorization|authorized|work permit|green card|citizen|right to work|eligible to work|work eligibility", text):
            return "authorization"
        if re.search(r"relocat\w*|commut\w*|hybrid|remote|onsite|on-site|in[- ]office|work setting|work mode|work arrangement", text):
            return "work_preference"
        if re.search(r"security clearance|government clearance|clearance", text):
            return "clearance"
        if re.search(r"criminal|background|disability|veteran|legal declaration", text):
            return "legal"
        if re.search(r"certification|certified|license", text):
            return "certification"
        if re.search(r"notice period|start date|join", text):
            return "notice"
        if re.search(r"city|location|where are you located|current location", text):
            return "location"
        if re.search(r"degree|bachelor|master|education|computer science|statistics|analytics", text):
            return "education"
        if re.search(r"years|how many", text):
            return "years"
        if any(skill.lower() in text for skill in ("sql", "python", "excel", "power bi", "tableau", "fastapi", "react", "tensorflow")):
            return "skill"
        return "yes_no"

    def _degree_option_from_resume(self, resume: ResumeProfile, options: list[str]) -> str | None:
        text = json.dumps(resume.education or [], default=str).lower() + "\n" + (resume.raw_text or "").lower()
        if not text.strip():
            return None
        option_map = [
            (("doctor", "phd", "ph.d"), ("doctor", "phd", "ph.d")),
            (("master", "m.sc", "msc", "mtech", "m.tech", "mba"), ("master", "mba", "ms ", "m.s", "postgraduate")),
            (("bachelor", "b.e", "be ", "btech", "b.tech", "b.sc", "bsc", "undergraduate", "engineering"), ("bachelor", "undergraduate", "bs ", "b.s", "be ", "b.e", "btech", "b.tech")),
            (("associate", "diploma"), ("associate", "diploma")),
            (("high school", "secondary"), ("high school", "secondary")),
        ]
        for resume_terms, option_terms in option_map:
            if any(term in text for term in resume_terms):
                for option in options:
                    option_lower = option.lower()
                    if any(term in option_lower for term in option_terms):
                        return option
        return None

    def _matching_saved_screening_answer(self, question: str, category: str, answers: list[dict]) -> dict | None:
        normalized = self._normalize_screening_question(question)
        for answer in answers:
            if answer.get("normalized_question") == normalized:
                return answer
        if category == "skill_years":
            skill = self._extract_skill_from_experience_question(question)
            if skill:
                for answer in answers:
                    evidence = answer.get("evidence") or {}
                    if (
                        answer.get("category") == "skill_years"
                        and str(evidence.get("normalized_skill") or "").lower() == skill.lower()
                    ):
                        return answer
        if category == "salary":
            for answer in answers:
                if answer.get("category") == "salary":
                    return answer
        return None

    def _normalize_screening_question(self, question: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
        aliases = {
            "will you require visa sponsorship": "work sponsorship",
            "will you now or in the future require sponsorship": "work sponsorship",
            "do you require sponsorship": "work sponsorship",
            "do you need employment sponsorship": "work sponsorship",
            "are you legally authorized to work in the united states": "us work authorization",
            "are you authorized to work in the united states": "us work authorization",
            "are you eligible to work in the united states": "us work authorization",
            "do you have a green card": "us work authorization",
            "are you a us citizen": "us work authorization",
            "are you a u s citizen": "us work authorization",
            "are you willing to relocate": "relocation willingness",
            "are you open to relocation": "relocation willingness",
            "are you comfortable commuting to this job s location": "commute willingness",
            "are you comfortable working in a hybrid setting": "work mode preference",
            "desired salary": "expected salary",
            "expected ctc": "expected salary",
            "compensation expectation": "expected salary",
        }
        return aliases.get(normalized, normalized[:512])

    def _skill_experience_screening_answer(
        self,
        question: str,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        screening_context: dict,
        mode: str,
    ) -> dict | None:
        skill = self._extract_skill_from_experience_question(question)
        if not skill:
            return None
        estimate = self._estimate_skill_years(skill, resume, candidate_profile)
        if estimate["found"] and estimate["confidence"] in {"High", "Medium"}:
            return self._screening_event(
                str(estimate["years"]),
                estimate["source"],
                estimate["confidence"],
                estimate["reason"],
                False,
                evidence=estimate,
            )
        if mode != "aggressive":
            return None
        if estimate["found"]:
            estimate["years"] = max(1, int(estimate["years"] or 1))
            estimate["aggressive_default_used"] = True
            return self._screening_event(
                str(estimate["years"]),
                estimate["source"],
                "Estimated",
                estimate["reason"],
                True,
                evidence=estimate,
            )
        estimate.update(
            {
                "years": 0,
                "source": "Aggressive Default",
                "confidence": "Estimated / Low",
                "aggressive_default_used": True,
                "reason": f"No evidence for {skill} was found; Aggressive Mode selected 0.",
            }
        )
        return self._screening_event(
            "0",
            "Aggressive Default",
            "Estimated / Low",
            estimate["reason"],
            True,
            evidence=estimate,
        )

    def _extract_skill_from_experience_question(self, question: str) -> str | None:
        text = re.sub(r"\s+", " ", question or "").strip()
        lowered = text.lower()
        if not re.search(r"years?|how long|experience", lowered):
            return None
        aliases = {
            "Microsoft Excel": ["microsoft excel", "ms excel", "excel"],
            "Excel": ["excel"],
            "Python": ["python (programming language)", "python programming language", "python"],
            "SQL": ["sql", "structured query language"],
            "Databases": ["databases", "database", "relational databases", "rdbms"],
            "Power BI": ["power bi", "powerbi"],
            "Tableau": ["tableau"],
            "FastAPI": ["fastapi", "fast api"],
            "React": ["react.js", "reactjs", "react"],
            "TensorFlow": ["tensorflow", "tensor flow"],
        }
        for canonical, variants in aliases.items():
            if any(re.search(rf"\b{re.escape(variant)}\b", lowered) for variant in variants):
                if canonical == "Microsoft Excel":
                    return "Excel"
                return canonical
        patterns = [
            r"(?:with|using|used)\s+([A-Za-z][A-Za-z0-9+#./ -]{1,40})\??",
            r"years\s+of\s+([A-Za-z][A-Za-z0-9+#./ -]{1,40})\s+experience",
            r"([A-Za-z][A-Za-z0-9+#./ -]{1,40})\s+experience\s+in\s+years",
        ]
        stop_words = {"work", "professional", "relevant", "total", "overall"}
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            candidate = re.sub(r"[^A-Za-z0-9+#. ]+", " ", match.group(1)).strip()
            candidate = re.sub(r"\s+", " ", candidate)
            if candidate and candidate.lower() not in stop_words:
                return candidate[:80]
        return None

    def _estimate_skill_years(
        self,
        skill: str,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
    ) -> dict:
        skill_pattern = self._skill_pattern(skill)
        evidence_texts = self._skill_evidence_texts(resume, candidate_profile)
        raw_text = resume.raw_text or ""
        dated_years = self._skill_years_from_dated_context(
            skill_pattern,
            "\n".join(
                [
                    raw_text,
                    "\n".join(json.dumps(item, default=str) for item in resume.experience or []),
                ]
            ),
        )
        if dated_years:
            return {
                "normalized_skill": skill,
                "selected_years": dated_years,
                "years": dated_years,
                "source": "Resume Experience Dates",
                "confidence": "High",
                "found": True,
                "aggressive_default_used": False,
                "evidence_used": f"Skill appeared near dated resume experience; calculated {dated_years} year(s).",
                "reason": "Calculated years from resume dates tied to the skill.",
            }
        stated_years = self._years_stated_for_skill(skill_pattern, raw_text)
        if stated_years:
            return {
                "normalized_skill": skill,
                "selected_years": stated_years,
                "years": stated_years,
                "source": "Resume",
                "confidence": "High",
                "found": True,
                "aggressive_default_used": False,
                "evidence_used": f"Resume states {stated_years} year(s) with the skill.",
                "reason": "Parsed an explicit 'N years' figure stated alongside the skill.",
            }
        if self._skill_in_structured_resume(skill_pattern, resume.experience):
            years = self._estimated_years_of_experience(resume) or 1
            return {
                "normalized_skill": skill,
                "selected_years": years,
                "years": years,
                "source": "Resume Experience",
                "confidence": "Medium",
                "found": True,
                "aggressive_default_used": False,
                "evidence_used": "Skill appeared in professional experience.",
                "reason": "Estimated from professional experience because exact skill dates were not available.",
            }
        if self._skill_in_texts(skill_pattern, evidence_texts["projects"]):
            return {
                "normalized_skill": skill,
                "selected_years": 1,
                "years": 1,
                "source": "Resume Projects",
                "confidence": "Estimated",
                "found": True,
                "aggressive_default_used": True,
                "evidence_used": "Skill appeared in resume project text.",
                "reason": "Skill appeared only in projects; used conservative 1 year estimate.",
            }
        if self._skill_in_texts(skill_pattern, evidence_texts["skills"]):
            return {
                "normalized_skill": skill,
                "selected_years": 1,
                "years": 1,
                "source": "Resume Skills",
                "confidence": "Estimated",
                "found": True,
                "aggressive_default_used": True,
                "evidence_used": "Skill appeared in resume skills or technologies.",
                "reason": "Skill appeared in skills section; used conservative 1 year estimate.",
            }
        if self._skill_in_texts(skill_pattern, evidence_texts["profile"]):
            return {
                "normalized_skill": skill,
                "selected_years": 1,
                "years": 1,
                "source": "Candidate Profile",
                "confidence": "Estimated",
                "found": True,
                "aggressive_default_used": True,
                "evidence_used": "Skill appeared in Candidate Profile text.",
                "reason": "Skill appeared in Candidate Profile; used conservative 1 year estimate.",
            }
        return {
            "normalized_skill": skill,
            "selected_years": None,
            "years": None,
            "source": None,
            "confidence": "None",
            "found": False,
            "aggressive_default_used": False,
            "evidence_used": "",
            "reason": f"No evidence for {skill} was found.",
        }

    def _years_stated_for_skill(self, skill_pattern: re.Pattern, text: str) -> int | None:
        """Parse an explicit 'N years' figure stated in the same sentence as the
        skill (e.g. '4 years of experience with SQL')."""
        if not text:
            return None
        for sentence in re.split(r"[.\n\r;]+", text):
            if not skill_pattern.search(sentence):
                continue
            match = re.search(r"(\d{1,2})\s*\+?\s*years?", sentence, re.I)
            if match:
                value = int(match.group(1))
                if 0 < value <= 60:
                    return value
        return None

    def _skill_pattern(self, skill: str) -> re.Pattern:
        aliases = {
            "Excel": r"(?:microsoft\s+excel|ms\s+excel|excel)",
            "Python": r"(?:python(?:\s+\(programming language\))?)",
            "SQL": r"(?:sql|structured\s+query\s+language)",
            "Databases": r"(?:databases?|relational\s+databases?|rdbms|sql|mysql|postgres(?:ql)?|sql\s+server)",
            "Power BI": r"(?:power\s*bi|powerbi)",
            "Tableau": r"tableau",
        }
        pattern = aliases.get(skill, re.escape(skill))
        return re.compile(rf"\b{pattern}\b", re.I)

    def _extract_graduation_year(self, resume: ResumeProfile) -> int | None:
        education_text = json.dumps(resume.education or [], default=str)
        candidates: list[int] = []
        for text in [education_text, resume.raw_text or ""]:
            for match in re.finditer(r"(?:graduat\w*|degree|bachelor|master|university|college|education).{0,80}\b((?:19|20)\d{2})\b", text, re.I | re.S):
                candidates.append(int(match.group(1)))
            for match in re.finditer(r"\b((?:19|20)\d{2})\b.{0,80}(?:graduat\w*|degree|bachelor|master|university|college|education)", text, re.I | re.S):
                candidates.append(int(match.group(1)))
        if not candidates:
            return None
        current_year = datetime.utcnow().year
        plausible = [year for year in candidates if 1970 <= year <= current_year + 8]
        return max(plausible) if plausible else None

    def _skill_evidence_texts(
        self,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
    ) -> dict[str, list[str]]:
        profile_parts = []
        if candidate_profile is not None:
            profile_parts = [
                candidate_profile.current_job_title or "",
                candidate_profile.professional_headline or "",
                candidate_profile.current_company or "",
                " ".join(candidate_profile.previous_companies or []),
            ]
        project_lines = [
            line
            for line in (resume.raw_text or "").splitlines()
            if re.search(r"project|built|developed|created|implemented", line, re.I)
        ]
        return {
            "skills": list(resume.skills or [])
            + list(resume.technologies or [])
            + list(resume.keywords or []),
            "projects": project_lines,
            "profile": profile_parts,
        }

    def _skill_in_structured_resume(self, skill_pattern: re.Pattern, items: list[dict]) -> bool:
        return any(skill_pattern.search(json.dumps(item, default=str)) for item in items or [])

    def _skill_in_texts(self, skill_pattern: re.Pattern, texts: list[str]) -> bool:
        return any(skill_pattern.search(str(text)) for text in texts or [])

    def _skill_years_from_dated_context(self, skill_pattern: re.Pattern, text: str) -> int | None:
        if not text or not skill_pattern.search(text):
            return None
        sentences = re.split(r"[\n\r]+", text)
        years: list[int] = []
        for sentence in sentences:
            if not skill_pattern.search(sentence):
                continue
            years.extend(self._years_from_date_ranges(sentence))
        return max(years) if years else None

    def _years_from_date_ranges(self, text: str) -> list[int]:
        current_year = datetime.utcnow().year
        results: list[int] = []
        month = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
        patterns = [
            rf"(?:{month}\s+)?(20\d{{2}})\s*(?:-|to|–|—)\s*(?:present|current|now|(?:{month}\s+)?(20\d{{2}}))",
            r"\b(20\d{2})\s*(?:-|to|–|—)\s*(present|current|now|20\d{2})\b",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                start = int(match.group(1))
                raw_end = match.group(2) if len(match.groups()) > 1 else None
                end = current_year if not raw_end or re.search(r"present|current|now", str(raw_end), re.I) else int(raw_end)
                if end >= start:
                    results.append(max(1, end - start))
        return results

    def _looks_yes_no_options(self, options: list[str]) -> bool:
        lowered = {option.lower() for option in options}
        return bool({"yes", "no"} & lowered)

    def _question_supported_by_resume(self, question: str, resume: ResumeProfile) -> bool:
        question_text = question.lower()
        evidence = " ".join(
            [
                resume.raw_text or "",
                " ".join(resume.skills or []),
                " ".join(resume.technologies or []),
                " ".join(resume.domain_expertise or []),
                " ".join(resume.job_titles or []),
            ]
        ).lower()
        tokens = [
            token
            for token in re.findall(r"[a-z][a-z0-9+#.]{2,}", question_text)
            if token not in {"have", "with", "experience", "related", "field", "degree", "company", "data"}
        ]
        if any(token in evidence for token in tokens):
            return True
        if "degree" in question_text and resume.education:
            return True
        return False

    def _estimated_years_of_experience(self, resume: ResumeProfile) -> int:
        text = resume.raw_text or ""
        matches = [int(value) for value in re.findall(r"(\d{1,2})\+?\s+years?", text, re.I)]
        return max(matches) if matches else 0

    def _extract_location_from_resume(self, resume: ResumeProfile) -> str | None:
        for keyword in ("Mumbai", "Pune", "Bengaluru", "Delhi", "New York", "Austin", "San Francisco"):
            if keyword.lower() in (resume.raw_text or "").lower():
                return keyword
        return None

    def _generated_short_screening_answer(self, question: str, resume: ResumeProfile, job: JobOpportunity) -> str:
        skills = ", ".join((resume.skills or resume.technologies or [])[:5])
        role = resume.detected_role or job.title
        if skills:
            return f"My background in {role} includes hands-on experience with {skills}, which aligns with this role."
        return f"My experience aligns with the {job.title} role and I am interested in contributing to {job.company}."

    def _extract_salary_from_text(self, text: str) -> dict | None:
        patterns = [
            (r"(?:INR|Rs\.?|₹)\s?([\d,.]+)", "INR"),
            (r"(?:USD|\$)\s?([\d,.]+)", "USD"),
            (r"(?:CAD)\s?([\d,.]+)", "CAD"),
            (r"(?:GBP|£)\s?([\d,.]+)", "GBP"),
            (r"(?:EUR|€)\s?([\d,.]+)", "EUR"),
        ]
        for pattern, currency in patterns:
            match = re.search(pattern, text or "", re.I)
            if match:
                return {"value": re.sub(r"[^\d]", "", match.group(1)), "currency": currency}
        return None

    def _estimate_salary(
        self,
        question: str,
        resume: ResumeProfile,
        job: JobOpportunity,
        candidate_profile: CandidateProfile | None,
    ) -> dict:
        currency = self._currency_for_location(job.location or (candidate_profile.current_location if candidate_profile else "") or "")
        title = (job.title or resume.detected_role or "").lower()
        years = self._estimated_years_of_experience(resume)
        base_by_currency = {
            "USD": 85000,
            "INR": 900000,
            "CAD": 85000,
            "GBP": 55000,
            "EUR": 65000,
        }
        base = base_by_currency.get(currency, 85000)
        if any(term in title for term in ("senior", "lead", "manager", "principal")):
            base = int(base * 1.45)
        elif any(term in title for term in ("intern", "junior", "entry")):
            base = int(base * 0.45)
        elif any(term in title for term in ("data scientist", "machine learning", "engineer")):
            base = int(base * 1.15)
        base = int(base + min(years, 10) * base * 0.04)
        if re.search(r"current", question, re.I):
            base = int(base * 0.9)
        if re.search(r"range", question, re.I):
            answer = f"{int(base * 0.9)}-{int(base * 1.1)}"
        else:
            answer = str(base)
        return {
            "answer": answer,
            "currency": currency,
            "estimated_base": base,
            "candidate_years": years,
            "job_title": job.title,
            "job_location": job.location,
            "work_type": self._work_type(job),
        }

    def _currency_for_location(self, location: str) -> str:
        text = (location or "").lower()
        if "india" in text or any(city in text for city in ("pune", "mumbai", "bengaluru", "delhi")):
            return "INR"
        if "canada" in text:
            return "CAD"
        if "united kingdom" in text or "uk" in text or "london" in text:
            return "GBP"
        if "europe" in text or any(country in text for country in ("germany", "france", "netherlands", "spain")):
            return "EUR"
        return "USD"

    def _work_type(self, job: JobOpportunity) -> str:
        text = f"{job.title} {job.location} {job.description}".lower()
        if "remote" in text:
            return "remote"
        if "hybrid" in text:
            return "hybrid"
        return "onsite"

