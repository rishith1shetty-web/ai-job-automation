import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import FormFieldLearning, GeneralizedFormFieldMetadata
from app.services.form_learning import FieldLearningInput, FormLearningEngine


@dataclass
class GeneralizedFieldInput:
    label: str = ""
    placeholder: str | None = None
    aria_label: str | None = None
    nearby_text: str | None = None
    validation_message: str | None = None
    dom_attributes: dict | None = None
    input_type: str | None = None
    tag_name: str | None = None
    role: str | None = None
    existing_value: str | None = None
    options: list[str] = field(default_factory=list)
    multiple: bool = False
    required: bool = False
    portal: str = "generic"


@dataclass
class GeneralizedFieldDetection:
    detected_control_type: str
    normalized_field_name: str
    field_category: str
    confidence: str
    recommended_fill_strategy: str
    evidence_used: dict
    source: str = "Generalized Form Engine"

    def to_dict(self) -> dict:
        return {
            "detected_control_type": self.detected_control_type,
            "normalized_field_name": self.normalized_field_name,
            "field_category": self.field_category,
            "confidence": self.confidence,
            "recommended_fill_strategy": self.recommended_fill_strategy,
            "evidence_used": self.evidence_used,
            "source": self.source,
        }


class PortalAdapter(Protocol):
    portal: str

    def normalize_input(self, field: GeneralizedFieldInput) -> GeneralizedFieldInput:
        ...


class GenericWebFormAdapter:
    portal = "generic"

    def normalize_input(self, field: GeneralizedFieldInput) -> GeneralizedFieldInput:
        field.portal = field.portal or self.portal
        return field


class LinkedInFormAdapter(GenericWebFormAdapter):
    portal = "linkedin"

    def normalize_input(self, field: GeneralizedFieldInput) -> GeneralizedFieldInput:
        field.portal = "linkedin"
        attrs = dict(field.dom_attributes or {})
        classes = str(attrs.get("class") or "")
        nearby = " ".join(item for item in [field.nearby_text or "", classes] if item).strip()
        if nearby:
            field.nearby_text = nearby
        return field


class GeneralizedFormEngine:
    FIELD_ALIASES = {
        "Salary": [
            "current ctc",
            "expected ctc",
            "current compensation",
            "annual salary",
            "salary expectation",
            "compensation expectation",
            "hourly rate",
            "pay rate",
            "salary",
        ],
        "Work Authorization": [
            "visa sponsorship",
            "right to work",
            "authorized to work",
            "work authorization",
            "work permit",
            "sponsor",
        ],
        "Employment Start Month": ["month of from", "start month", "from month"],
        "Employment Start Year": ["year of from", "start year", "from year"],
        "Employment End Month": ["month of to", "end month", "to month"],
        "Employment End Year": ["year of to", "end year", "to year"],
        "Mobile Phone": ["mobile phone", "phone number", "telephone"],
        "Phone Country Code": ["phone country code", "country code"],
        "Email": ["email address", "email"],
        "Location": ["location city", "city", "current location", "where are you located"],
        "Resume Upload": ["resume", "cv", "upload resume", "upload cv"],
    }
    CATEGORY_BY_FIELD = {
        "Salary": "Salary",
        "Work Authorization": "Work Authorization",
        "Employment Start Month": "Employment",
        "Employment Start Year": "Employment",
        "Employment End Month": "Employment",
        "Employment End Year": "Employment",
        "Mobile Phone": "Contact",
        "Phone Country Code": "Contact",
        "Email": "Contact",
        "Location": "Contact",
        "Resume Upload": "Resume Upload",
    }
    STRATEGIES = {
        "textbox": "type_value",
        "textarea": "type_longer_answer",
        "dropdown": "select_matching_option",
        "searchable_dropdown": "type_value_wait_select_suggestion",
        "autocomplete": "type_value_select_best_suggestion",
        "checkbox": "check_or_uncheck",
        "radio": "select_matching_option",
        "date_picker": "open_picker_select_date",
        "month_picker": "select_month",
        "year_picker": "select_year",
        "numeric_input": "enter_number_only",
        "file_upload": "use_existing_resume_upload_logic",
        "multi_select": "select_multiple_matching_options",
    }

    def __init__(
        self,
        settings: Settings,
        form_learning: FormLearningEngine | None = None,
        adapters: dict[str, PortalAdapter] | None = None,
    ) -> None:
        self.settings = settings
        self.form_learning = form_learning or FormLearningEngine(settings)
        self.adapters = adapters or {
            "generic": GenericWebFormAdapter(),
            "linkedin": LinkedInFormAdapter(),
        }

    def create_debug_dir(self) -> Path:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        debug_dir = self.settings.generalized_form_debug_dir / timestamp
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def detect_field(self, field: GeneralizedFieldInput, learned: FormFieldLearning | None = None) -> GeneralizedFieldDetection:
        adapter = self.adapters.get(field.portal) or self.adapters["generic"]
        field = adapter.normalize_input(field)
        learned_detection = self._detection_from_learning(field, learned)
        if learned_detection:
            return learned_detection
        normalized = self._normalize_field_name(field)
        control = self._detect_control_type(field, normalized)
        category = self._field_category(normalized, field)
        confidence = self._confidence(field, normalized, control)
        return GeneralizedFieldDetection(
            detected_control_type=control,
            normalized_field_name=normalized,
            field_category=category,
            confidence=confidence,
            recommended_fill_strategy=self.STRATEGIES[control],
            evidence_used=self._evidence(field, learned=None, control=control, normalized=normalized),
        )

    def detect_fields(self, fields: list[GeneralizedFieldInput], learned_records: list[FormFieldLearning] | None = None) -> list[GeneralizedFieldDetection]:
        learned_by_label = {
            self._normalize_text(item.original_label): item
            for item in learned_records or []
        }
        return [
            self.detect_field(field, learned_by_label.get(self._normalize_text(field.label)))
            for field in fields
        ]

    def record_metadata(
        self,
        db: Session,
        field: GeneralizedFieldInput,
        detection: GeneralizedFieldDetection | None = None,
        *,
        success: bool | None = None,
        validation_message: str | None = None,
    ) -> GeneralizedFormFieldMetadata:
        detection = detection or self.detect_field(field)
        record = self._pending_metadata_record(db, field, detection) or db.scalar(
            select(GeneralizedFormFieldMetadata)
            .where(GeneralizedFormFieldMetadata.portal == field.portal)
            .where(GeneralizedFormFieldMetadata.original_field_label == field.label)
            .where(GeneralizedFormFieldMetadata.normalized_field_name == detection.normalized_field_name)
            .limit(1)
        )
        now = datetime.utcnow()
        if record is None:
            record = GeneralizedFormFieldMetadata(
                portal=field.portal,
                original_field_label=field.label,
                normalized_field_name=detection.normalized_field_name,
                field_category=detection.field_category,
                detected_control_type=detection.detected_control_type,
                recommended_fill_strategy=detection.recommended_fill_strategy,
                created_at=now,
            )
        record.times_seen = int(record.times_seen or 0) + 1
        if success is True:
            record.times_successful = int(record.times_successful or 0) + 1
        if success is False:
            record.times_failed = int(record.times_failed or 0) + 1
        record.confidence = self._updated_metadata_confidence(record, detection.confidence)
        record.last_validation_message = validation_message
        record.evidence_used = detection.evidence_used
        record.last_used_at = now
        record.updated_at = now
        db.add(record)
        return record

    def _pending_metadata_record(
        self,
        db: Session,
        field: GeneralizedFieldInput,
        detection: GeneralizedFieldDetection,
    ) -> GeneralizedFormFieldMetadata | None:
        for item in db.new:
            if not isinstance(item, GeneralizedFormFieldMetadata):
                continue
            if (
                item.portal == field.portal
                and item.original_field_label == field.label
                and item.normalized_field_name == detection.normalized_field_name
            ):
                return item
        return None

    def write_debug(
        self,
        debug_dir: Path,
        fields: list[GeneralizedFieldInput],
        detections: list[GeneralizedFieldDetection],
        *,
        learning_reuse: list[dict] | None = None,
        validation_results: list[dict] | None = None,
    ) -> None:
        payload = [item.to_dict() for item in detections]
        files = {
            "detected_fields.json": payload,
            "control_types.json": [
                {
                    "field": detection.normalized_field_name,
                    "detected_control_type": detection.detected_control_type,
                    "evidence_used": detection.evidence_used,
                }
                for detection in detections
            ],
            "fill_strategies.json": [
                {
                    "field": detection.normalized_field_name,
                    "strategy": detection.recommended_fill_strategy,
                    "note": "Strategy only; value must come from unified decision pipeline.",
                }
                for detection in detections
            ],
            "learning_reuse.json": learning_reuse or [
                detection.evidence_used.get("learning_reuse", {})
                for detection in detections
                if detection.evidence_used.get("learning_reuse")
            ],
            "validation_results.json": validation_results or [],
            "pipeline_integration.json": {
                "engine_role": "control_detection_only",
                "value_source": "Unified decision pipeline",
                "decision_order": ["saved_answer", "learned_mapping", "evidence", "reasoning", "estimator", "fallback"],
                "fields": [
                    {
                        "label": field.label,
                        "normalized_field_name": detection.normalized_field_name,
                        "recommended_fill_strategy": detection.recommended_fill_strategy,
                    }
                    for field, detection in zip(fields, detections)
                ],
            },
        }
        for name, data in files.items():
            (debug_dir / name).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _detection_from_learning(self, field: GeneralizedFieldInput, learned: FormFieldLearning | None) -> GeneralizedFieldDetection | None:
        if not learned:
            return None
        control = self._control_type_from_learning(field, learned)
        return GeneralizedFieldDetection(
            detected_control_type=control,
            normalized_field_name=learned.normalized_field,
            field_category=learned.field_category,
            confidence=learned.confidence,
            recommended_fill_strategy=self.STRATEGIES[control],
            evidence_used=self._evidence(field, learned=learned, control=control, normalized=learned.normalized_field),
            source="AI Form Learning Reuse",
        )

    def _control_type_from_learning(self, field: GeneralizedFieldInput, learned: FormFieldLearning) -> str:
        field_type = (learned.field_type or field.input_type or "").lower()
        if field_type in {"select", "select-one", "dropdown"}:
            return "dropdown"
        if field_type in {"checkbox", "radio", "textarea", "file"}:
            return "file_upload" if field_type == "file" else field_type
        return self._detect_control_type(field, learned.normalized_field)

    def _detect_control_type(self, field: GeneralizedFieldInput, normalized_field: str) -> str:
        text = self._combined_text(field)
        input_type = self._normalize_text(field.input_type or "")
        tag = self._normalize_text(field.tag_name or "")
        role = self._normalize_text(field.role or "")
        attrs = {str(key).lower(): str(value).lower() for key, value in (field.dom_attributes or {}).items()}
        class_text = attrs.get("class", "")
        if input_type == "file" or normalized_field == "Resume Upload":
            return "file_upload"
        if normalized_field in {"Mobile Phone", "Phone Country Code"} and tag != "select":
            return "textbox"
        if field.multiple or "multiple" in attrs or "multi" in text:
            return "multi_select"
        if input_type in {"checkbox"} or role == "checkbox":
            return "checkbox"
        if input_type == "radio" or role == "radio":
            return "radio"
        if tag == "textarea":
            return "textarea"
        if normalized_field.endswith("Years"):
            return "numeric_input"
        if "month" in normalized_field.lower() or input_type == "month":
            return "month_picker"
        if "year" in normalized_field.lower():
            return "year_picker"
        if tag == "select" or input_type in {"select", "select-one"}:
            if "search" in class_text or "search" in text or "combobox" in role:
                return "searchable_dropdown"
            return "dropdown"
        if role == "combobox" or "autocomplete" in attrs.get("aria-autocomplete", ""):
            return "autocomplete"
        if input_type in {"date", "datetime-local"} or "date picker" in text:
            return "date_picker"
        if input_type in {"number", "tel"} or re.search(r"\b(number|numeric|years?|ctc|salary|amount)\b", text):
            return "numeric_input"
        if tag == "textarea" or len(field.options or []) == 0 and any(term in text for term in ("describe", "summary", "cover letter", "explain")):
            return "textarea"
        return "textbox"

    def _normalize_field_name(self, field: GeneralizedFieldInput) -> str:
        text = self._combined_text(field)
        for normalized, aliases in self.FIELD_ALIASES.items():
            if any(alias in text for alias in aliases):
                return normalized
        learned = self.form_learning.evaluate_field(
            FieldLearningInput(
                label=field.label or "Unknown Field",
                placeholder=field.placeholder,
                nearby_text=field.nearby_text,
                input_type=field.input_type,
                dom_attributes=field.dom_attributes,
                validation_messages=[field.validation_message] if field.validation_message else None,
            )
        )
        return learned.normalized_field

    def _field_category(self, normalized_field: str, field: GeneralizedFieldInput) -> str:
        if normalized_field in self.CATEGORY_BY_FIELD:
            return self.CATEGORY_BY_FIELD[normalized_field]
        return self.form_learning.classify_field(normalized_field, self._combined_text(field))

    def _confidence(self, field: GeneralizedFieldInput, normalized: str, control: str) -> str:
        score = 0
        if field.label:
            score += 30
        if field.placeholder:
            score += 15
        if field.aria_label:
            score += 15
        if field.options:
            score += 15
        if field.dom_attributes:
            score += 10
        if normalized != "Unknown Field":
            score += 15
        if control in self.STRATEGIES:
            score += 10
        if score >= 80:
            return "High"
        if score >= 55:
            return "Medium"
        return "Low"

    def _evidence(
        self,
        field: GeneralizedFieldInput,
        *,
        learned: FormFieldLearning | None,
        control: str,
        normalized: str,
    ) -> dict:
        evidence = {
            "label": field.label,
            "placeholder": field.placeholder,
            "aria_label": field.aria_label,
            "nearby_text": field.nearby_text,
            "validation_message": field.validation_message,
            "input_type": field.input_type,
            "tag_name": field.tag_name,
            "role": field.role,
            "existing_value_present": bool(field.existing_value),
            "options_count": len(field.options or []),
            "dom_attribute_keys": sorted((field.dom_attributes or {}).keys()),
            "detected_control_type": control,
            "normalized_field_name": normalized,
        }
        if learned:
            evidence["learning_reuse"] = {
                "record_id": learned.id,
                "times_seen": learned.times_seen,
                "times_successful": learned.times_successful,
                "times_failed": learned.times_failed,
                "confidence": learned.confidence,
            }
        return evidence

    def _updated_metadata_confidence(self, record: GeneralizedFormFieldMetadata, fallback: str) -> str:
        seen = max(int(record.times_seen or 0), 1)
        success_rate = int(record.times_successful or 0) / seen
        if int(record.times_successful or 0) >= 5 and success_rate >= 0.9:
            return "Very High"
        if int(record.times_successful or 0) >= 2 and success_rate >= 0.75:
            return "High"
        if int(record.times_failed or 0) > int(record.times_successful or 0):
            return "Very Low"
        return fallback

    def _combined_text(self, field: GeneralizedFieldInput) -> str:
        return self._normalize_text(
            " ".join(
                [
                    field.label or "",
                    field.placeholder or "",
                    field.aria_label or "",
                    field.nearby_text or "",
                    field.validation_message or "",
                    field.input_type or "",
                    field.tag_name or "",
                    field.role or "",
                    " ".join(field.options or []),
                    json.dumps(field.dom_attributes or {}, default=str),
                ]
            )
        )

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip()
