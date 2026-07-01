import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import FormFieldLearning, JobOpportunity
from app.services.linkedin import AutomationResult
from app.services.reasoning import AIReasoningEngine


@dataclass
class FieldLearningInput:
    label: str
    placeholder: str | None = None
    nearby_text: str | None = None
    input_type: str | None = None
    dom_attributes: dict | None = None
    validation_messages: list[str] | None = None
    company: str | None = None
    role: str | None = None
    job_description: str | None = None
    reasoning_output: dict | None = None
    candidate_context: dict | None = None


@dataclass
class FieldLearningDecision:
    normalized_field: str
    field_category: str
    confidence: str
    suggested_value_source: str
    reasoning_summary: str


class FormLearningEngine:
    CATEGORY_PATTERNS = {
        "Contact": ["first name", "last name", "email", "phone", "mobile", "city", "location", "address"],
        "Employment": ["company", "employer", "job title", "title", "start date", "end date", "currently work", "description"],
        "Education": ["degree", "school", "university", "college", "education", "graduation"],
        "Salary": ["salary", "ctc", "compensation", "package", "pay", "rate"],
        "Work Authorization": ["visa", "sponsor", "authorization", "authorized", "right to work", "work permit"],
        "Certification": ["certification", "certified", "license"],
        "Projects": ["project", "portfolio", "case study"],
        "Languages": ["language", "speak", "fluent"],
        "Cover Letter": ["cover letter", "why interested", "why are you interested"],
        "Resume Upload": ["resume", "cv", "upload"],
        "Notice Period": ["notice", "start date", "availability", "available"],
        "Availability": ["availability", "available", "join"],
        "Free Text": ["describe", "tell us", "explain", "summary", "about yourself"],
    }
    SKILL_ALIASES = {
        "SQL": ["sql", "structured query language"],
        "Python": ["python"],
        "Excel": ["excel", "microsoft excel", "ms excel"],
        "Power BI": ["power bi", "powerbi"],
        "Tableau": ["tableau"],
        "FastAPI": ["fastapi", "fast api"],
        "React": ["react", "react.js", "reactjs"],
        "Machine Learning": ["machine learning", "ml"],
    }

    def __init__(self, settings: Settings, reasoning: AIReasoningEngine | None = None):
        self.settings = settings
        self.reasoning = reasoning or AIReasoningEngine(settings)

    def create_debug_dir(self) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        debug_dir = self.settings.form_learning_debug_dir / timestamp
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def evaluate_field(self, field: FieldLearningInput) -> FieldLearningDecision:
        text = self._combined_text(field)
        normalized = self.normalize_field(text)
        category = self.classify_field(normalized, text)
        confidence = "High" if normalized != self._title(field.label) else "Medium"
        source = self.suggested_value_source(category, normalized)
        summary = (
            f"Normalized {field.label!r} to {normalized!r} using label, placeholder, nearby text, "
            f"DOM attributes, validation messages, job context, and existing reasoning output."
        )
        return FieldLearningDecision(normalized, category, confidence, source, summary)

    def normalize_field(self, text: str) -> str:
        value = self._normalize_text(text)
        skill = self._skill_years(value)
        if skill:
            return f"{skill} Years"
        if any(term in value for term in ("current salary", "current ctc", "current compensation", "annual salary", "current package", "expected ctc", "salary", "compensation", "hourly rate")):
            return "Salary"
        if any(term in value for term in ("visa sponsorship", "work authorization", "right to work", "authorized to work", "work permit", "sponsor")):
            return "Work Authorization"
        if "notice" in value:
            return "Notice Period"
        if "cover letter" in value or "why are you interested" in value:
            return "Cover Letter"
        if "resume" in value or "cv" in value:
            return "Resume Upload"
        contact = self._contact_normalization(value)
        if contact:
            return contact
        employment = self._employment_normalization(value)
        if employment:
            return employment
        education = self._education_normalization(value)
        if education:
            return education
        return self._title(text)

    def classify_field(self, normalized_field: str, text: str) -> str:
        if normalized_field.endswith(" Years"):
            return "Skill Years"
        if normalized_field in {"Salary", "Work Authorization", "Notice Period", "Cover Letter", "Resume Upload"}:
            return normalized_field
        lowered = self._normalize_text(f"{normalized_field} {text}")
        for category, terms in self.CATEGORY_PATTERNS.items():
            if any(term in lowered for term in terms):
                return category
        if re.search(r"years?|experience", lowered):
            return "Experience"
        return "Free Text"

    def suggested_value_source(self, category: str, normalized_field: str) -> str:
        if category == "Contact":
            return "Candidate Profile / LinkedIn Prefill"
        if category == "Employment":
            return "Employment History"
        if category == "Education":
            return "Resume Education"
        if category == "Salary":
            return "Candidate Knowledge Base / AI Reasoning"
        if category == "Skill Years":
            return "Skill Experience Estimator"
        if category in {"Work Authorization", "Certification"}:
            return "Candidate Knowledge Base"
        if category in {"Cover Letter", "Free Text", "Projects"}:
            return "Resume Intelligence / AI Reasoning"
        return "AI Reasoning Engine"

    def record_observation(
        self,
        db: Session,
        field: FieldLearningInput,
        *,
        success: bool,
        value_source: str | None = None,
        validation_message: str | None = None,
        used_existing_mapping: bool = False,
    ) -> FormFieldLearning:
        decision = self.evaluate_field(field)
        db.flush()
        record = db.scalar(
            select(FormFieldLearning)
            .where(FormFieldLearning.original_label == field.label)
            .where(FormFieldLearning.normalized_field == decision.normalized_field)
            .where(FormFieldLearning.company == field.company)
            .where(FormFieldLearning.role == field.role)
            .limit(1)
        )
        now = datetime.utcnow()
        if record is None:
            record = FormFieldLearning(
                original_label=field.label,
                normalized_field=decision.normalized_field,
                field_category=decision.field_category,
                field_type=field.input_type,
                company=field.company,
                role=field.role,
                first_seen=now,
            )
        record.times_seen = int(record.times_seen or 0) + 1
        record.times_used = int(record.times_used or 0) + int(bool(used_existing_mapping))
        if success:
            record.times_successful = int(record.times_successful or 0) + 1
            record.last_successful_value_source = value_source or decision.suggested_value_source
        else:
            record.times_failed = int(record.times_failed or 0) + 1
            if validation_message:
                examples = list(record.validation_examples or [])
                examples.append(
                    {
                        "message": validation_message,
                        "reason": "LinkedIn validation or unresolved required field.",
                        "suggested_fix": self.suggested_fix(decision, validation_message),
                        "seen_at": now.isoformat(),
                    }
                )
                record.validation_examples = examples[-20:]
        reasoning = list(record.reasoning_used or [])
        reasoning.append(
            {
                "normalized_field": decision.normalized_field,
                "field_category": decision.field_category,
                "reasoning_summary": decision.reasoning_summary,
                "reasoning_output": field.reasoning_output or {},
                "candidate_context_keys": sorted((field.candidate_context or {}).keys()),
                "seen_at": now.isoformat(),
            }
        )
        record.reasoning_used = reasoning[-20:]
        record.suggested_value_source = decision.suggested_value_source
        record.reasoning_summary = decision.reasoning_summary
        record.confidence = self.updated_confidence(record)
        record.last_seen = now
        db.add(record)
        return record

    def learn_from_application_result(
        self,
        db: Session,
        job: JobOpportunity,
        result: AutomationResult,
    ) -> list[FormFieldLearning]:
        learned: list[FormFieldLearning] = []
        filled_by_label = {
            item.get("field_label"): item
            for item in result.contact_fields_filled
            if item.get("field_label")
        }
        for event in result.contact_fields_detected:
            label = event.get("field_label")
            if not label:
                continue
            filled = filled_by_label.get(label, {})
            success = bool(filled) or bool(event.get("existing_value_present"))
            learned.append(
                self.record_observation(
                    db,
                    FieldLearningInput(
                        label=label,
                        input_type=event.get("field_type"),
                        company=job.company,
                        role=job.title,
                        job_description=job.description,
                        candidate_context={"contact_key": event.get("contact_key")},
                    ),
                    success=success,
                    value_source=filled.get("value_source") or ("LinkedIn Prefill" if event.get("existing_value_present") else None),
                    validation_message=None if success else "Required contact value was not available or could not be filled.",
                )
            )
        for event in result.screening_answers or []:
            label = event.get("question_text")
            if not label:
                continue
            learned.append(
                self.record_observation(
                    db,
                    FieldLearningInput(
                        label=label,
                        input_type=event.get("field_type"),
                        nearby_text=" ".join(event.get("options") or []),
                        company=job.company,
                        role=job.title,
                        job_description=job.description,
                        reasoning_output={
                            "reasoning_type": event.get("reasoning_type"),
                            "confidence": event.get("confidence"),
                            "reason": event.get("reason"),
                        },
                    ),
                    success=bool(event.get("filled_successfully")),
                    value_source=event.get("source"),
                    validation_message=event.get("error") if not event.get("filled_successfully") else None,
                )
            )
        for event in self._employment_events(result):
            label = event.get("field_label")
            if not label:
                continue
            learned.append(
                self.record_observation(
                    db,
                    FieldLearningInput(
                        label=label,
                        input_type=event.get("field_type"),
                        company=job.company,
                        role=job.title,
                        job_description=job.description,
                        candidate_context={"field_key": event.get("field_key")},
                    ),
                    success=bool(event.get("success")),
                    value_source=event.get("source"),
                    validation_message=event.get("reason") if not event.get("success") else None,
                )
            )
        self.write_debug(self.create_debug_dir(), learned)
        return learned

    def updated_confidence(self, record: FormFieldLearning) -> str:
        seen = max(int(record.times_seen or 0), 1)
        success_rate = int(record.times_successful or 0) / seen
        if int(record.times_successful or 0) >= 5 and success_rate >= 0.9:
            return "Very High"
        if int(record.times_successful or 0) >= 2 and success_rate >= 0.75:
            return "High"
        if success_rate >= 0.5:
            return "Medium"
        if int(record.times_failed or 0) > int(record.times_successful or 0):
            return "Very Low"
        return "Low"

    def suggested_fix(self, decision: FieldLearningDecision, validation_message: str) -> str:
        if decision.field_category == "Skill Years":
            return "Confirm skill years in Candidate Knowledge Base or resume experience."
        if decision.field_category == "Employment":
            return "Review extracted employment history dates and required work-history fields."
        if decision.field_category == "Contact":
            return "Confirm Candidate Profile contact value or LinkedIn prefilled value."
        if "required" in validation_message.lower():
            return f"Provide a reliable value source for {decision.normalized_field}."
        return "Inspect field format and update the learned mapping if needed."

    def write_debug(self, debug_dir: Path, learned: list[FormFieldLearning]) -> None:
        payload = [self.record_payload(item) for item in learned]
        validation = [
            {
                "normalized_field": item.normalized_field,
                "validation_examples": item.validation_examples,
            }
            for item in learned
            if item.validation_examples
        ]
        confidence = [
            {
                "normalized_field": item.normalized_field,
                "confidence": item.confidence,
                "times_seen": item.times_seen,
                "times_successful": item.times_successful,
                "times_failed": item.times_failed,
            }
            for item in learned
        ]
        files = {
            "learned_fields.json": payload,
            "field_normalization.json": [
                {"original_label": item.original_label, "normalized_field": item.normalized_field}
                for item in learned
            ],
            "validation_learning.json": validation,
            "confidence_updates.json": confidence,
            "field_statistics.json": payload,
            "reasoning_usage.json": [
                {"normalized_field": item.normalized_field, "reasoning_used": item.reasoning_used}
                for item in learned
            ],
        }
        for filename, data in files.items():
            (debug_dir / filename).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def record_payload(self, record: FormFieldLearning) -> dict:
        return {
            "id": record.id,
            "original_label": record.original_label,
            "normalized_field": record.normalized_field,
            "field_category": record.field_category,
            "field_type": record.field_type,
            "company": record.company,
            "role": record.role,
            "times_seen": record.times_seen,
            "times_successful": record.times_successful,
            "times_failed": record.times_failed,
            "times_used": record.times_used,
            "success_rate": self.success_rate(record),
            "confidence": record.confidence,
            "validation_examples": record.validation_examples or [],
            "last_successful_value_source": record.last_successful_value_source,
            "suggested_value_source": record.suggested_value_source,
            "reasoning_summary": record.reasoning_summary,
            "first_seen": record.first_seen,
            "last_seen": record.last_seen,
        }

    def success_rate(self, record: FormFieldLearning) -> float:
        seen = int(record.times_seen or 0)
        return 0.0 if seen == 0 else round((int(record.times_successful or 0) / seen) * 100, 2)

    def _employment_events(self, result: AutomationResult) -> list[dict]:
        events: list[dict] = []
        for log_event in result.log or []:
            if log_event.get("event") != "employment_history_autofill":
                continue
            try:
                parsed = json.loads(log_event.get("detail") or "[]")
            except (TypeError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                events.extend(item for item in parsed if isinstance(item, dict))
        return events

    def _skill_years(self, text: str) -> str | None:
        if not re.search(r"years?|experience|used|using", text):
            return None
        for skill, aliases in self.SKILL_ALIASES.items():
            if any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases):
                return skill
        return None

    def _contact_normalization(self, text: str) -> str | None:
        if "first name" in text:
            return "First Name"
        if "last name" in text:
            return "Last Name"
        if "email" in text:
            return "Email"
        if "country code" in text:
            return "Phone Country Code"
        if "phone" in text or "mobile" in text:
            return "Mobile Phone"
        if "city" in text or "location" in text:
            return "Location"
        return None

    def _employment_normalization(self, text: str) -> str | None:
        if "company" in text or "employer" in text:
            return "Company"
        if "job title" in text or text == "title" or "your title" in text:
            return "Job Title"
        if "month of from" in text or "start month" in text:
            return "Start Month"
        if "year of from" in text or "start year" in text:
            return "Start Year"
        if "month of to" in text or "end month" in text:
            return "End Month"
        if "year of to" in text or "end year" in text:
            return "End Year"
        if "currently work" in text:
            return "Currently Work Here"
        return None

    def _education_normalization(self, text: str) -> str | None:
        if "degree" in text:
            return "Degree"
        if "school" in text or "university" in text or "college" in text:
            return "School"
        return None

    def _combined_text(self, field: FieldLearningInput) -> str:
        return self._normalize_text(
            " ".join(
                [
                    field.label or "",
                    field.placeholder or "",
                    field.nearby_text or "",
                    field.input_type or "",
                    json.dumps(field.dom_attributes or {}, default=str),
                    " ".join(field.validation_messages or []),
                    field.job_description or "",
                    json.dumps(field.reasoning_output or {}, default=str),
                ]
            )
        )

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip()

    def _title(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9+#. ]+", " ", str(value or "")).strip()
        return re.sub(r"\s+", " ", cleaned).title() or "Unknown Field"
