from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApplicationStatus(str, Enum):
    MATCHED = "matched"
    QUEUED = "queued"
    APPLIED = "applied"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"
    FAILED = "failed"
    # Dry-run reached the submit/review stage successfully without submitting.
    DRY_RUN_READY = "dry_run_ready"
    # Form could not be automated safely (e.g. external ATS / unhandled fields).
    UNSUPPORTED_FORM = "unsupported_form"


class ResumeProfile(Base):
    __tablename__ = "resume_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    portal: Mapped[str] = mapped_column(String(50), default="linkedin", index=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(1024))
    raw_text: Mapped[str] = mapped_column(Text)
    # SHA-256 of the normalized parsed resume text — a stable identity for the same
    # resume across repeated uploads (which may receive different ids). Nullable so
    # legacy/unfingerprintable rows are left untouched. See services/resume_identity.py.
    resume_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    experience: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    education: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    job_titles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSONB, default=list)
    technologies: Mapped[list[str]] = mapped_column(JSONB, default=list)
    domain_expertise: Mapped[list[str]] = mapped_column(JSONB, default=list)
    detected_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_suggestions: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    generated_search_keywords: Mapped[list[str]] = mapped_column(JSONB, default=list)
    generated_search_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    role_confidence: Mapped[float] = mapped_column(Float, default=0)
    # Phase 2/3 analysis, persisted so Find Matches (and the dashboard) reuse the exact same domain
    # and 12-role ranking after a page refresh or app restart -- without re-uploading the resume.
    # JSONB defaulting to {} keeps legacy rows readable (they simply carry an empty profile).
    domain_profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    role_profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    jobs: Mapped[list["JobOpportunity"]] = relationship(back_populates="resume")
    search_batches: Mapped[list["JobSearchBatch"]] = relationship(back_populates="resume")


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    portal: Mapped[str] = mapped_column(String(50), default="linkedin", index=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mobile_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phone_country_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    portfolio_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    github_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    current_job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    professional_headline: Mapped[str | None] = mapped_column(String(512), nullable=True)
    current_company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    previous_companies: Mapped[list[str]] = mapped_column(JSONB, default=list)
    certifications: Mapped[list[str]] = mapped_column(JSONB, default=list)
    professional_links: Mapped[dict] = mapped_column(JSONB, default=dict)
    field_sources: Mapped[dict] = mapped_column(JSONB, default=dict)
    missing_required_after_apply: Mapped[list[str]] = mapped_column(JSONB, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def missing_required_fields(self) -> list[str]:
        required = {
            "First name": self.first_name,
            "Last name": self.last_name,
            "Email address": self.email,
            "Mobile phone number": self.mobile_phone,
            "Phone country code": self.phone_country_code,
            "Current city/location": self.current_location,
            "LinkedIn profile URL": self.linkedin_url,
        }
        return [label for label, value in required.items() if not (value or "").strip()]

    @property
    def is_complete(self) -> bool:
        return not self.missing_required_fields


class CandidateEmploymentHistory(Base):
    __tablename__ = "candidate_employment_history"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    candidate_profile_id: Mapped[int | None] = mapped_column(ForeignKey("candidate_profiles.id"), nullable=True, index=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resume_profiles.id"), nullable=True, index=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    start_month: Mapped[str | None] = mapped_column(String(30), nullable=True)
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_month: Mapped[str | None] = mapped_column(String(30), nullable=True)
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currently_work_here: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    skills_used: Mapped[list[str]] = mapped_column(JSONB, default=list)
    technologies_used: Mapped[list[str]] = mapped_column(JSONB, default=list)
    duration_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(120), default="Resume Experience")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScreeningAutomationSetting(Base):
    __tablename__ = "screening_automation_settings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    mode: Mapped[str] = mapped_column(String(30), default="safe")
    notice_period_default: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScreeningKnowledgeAnswer(Base):
    __tablename__ = "screening_knowledge_answers"
    __table_args__ = (
        UniqueConstraint(
            "candidate_profile_id",
            "normalized_question",
            name="uq_screening_answer_candidate_question",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    candidate_profile_id: Mapped[int | None] = mapped_column(ForeignKey("candidate_profiles.id"), nullable=True, index=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resume_profiles.id"), nullable=True, index=True)
    question_text: Mapped[str] = mapped_column(Text)
    normalized_question: Mapped[str] = mapped_column(String(512), index=True)
    category: Mapped[str] = mapped_column(String(100), default="general", index=True)
    answer: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100), default="Manual")
    confidence: Mapped[str] = mapped_column(String(50), default="High")
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FormFieldLearning(Base):
    __tablename__ = "form_field_learning"
    __table_args__ = (
        UniqueConstraint(
            "original_label",
            "normalized_field",
            "company",
            "role",
            name="uq_form_field_learning_mapping",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    original_label: Mapped[str] = mapped_column(Text)
    normalized_field: Mapped[str] = mapped_column(String(255), index=True)
    field_category: Mapped[str] = mapped_column(String(100), index=True)
    field_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    role: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    times_seen: Mapped[int] = mapped_column(Integer, default=0)
    times_successful: Mapped[int] = mapped_column(Integer, default=0)
    times_failed: Mapped[int] = mapped_column(Integer, default=0)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[str] = mapped_column(String(50), default="Low")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    validation_examples: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    reasoning_used: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    last_successful_value_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    suggested_value_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reasoning_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class GeneralizedFormFieldMetadata(Base):
    __tablename__ = "generalized_form_field_metadata"
    __table_args__ = (
        UniqueConstraint(
            "portal",
            "original_field_label",
            "normalized_field_name",
            name="uq_generalized_form_field_metadata",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    portal: Mapped[str] = mapped_column(String(100), default="linkedin", index=True)
    original_field_label: Mapped[str] = mapped_column(Text)
    normalized_field_name: Mapped[str] = mapped_column(String(255), index=True)
    field_category: Mapped[str] = mapped_column(String(100), default="general", index=True)
    detected_control_type: Mapped[str] = mapped_column(String(100), index=True)
    recommended_fill_strategy: Mapped[str] = mapped_column(String(100))
    confidence: Mapped[str] = mapped_column(String(50), default="Low")
    times_seen: Mapped[int] = mapped_column(Integer, default=0)
    times_successful: Mapped[int] = mapped_column(Integer, default=0)
    times_failed: Mapped[int] = mapped_column(Integer, default=0)
    last_validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_used: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class JobSearchBatch(Base):
    __tablename__ = "job_search_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resume_profiles.id"), index=True)
    search_query: Mapped[str] = mapped_column(Text)
    manual_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str] = mapped_column(String(255))
    portal: Mapped[str] = mapped_column(String(50), default="linkedin", index=True)
    status: Mapped[str] = mapped_column(String(50), default="completed", index=True)
    requested_limit: Mapped[int] = mapped_column(Integer, default=50)
    current_search_variation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Live per-variation progress, published identically by every portal (1-based index).
    current_variation_index: Mapped[int] = mapped_column(Integer, default=0)
    total_variations: Mapped[int] = mapped_column(Integer, default=0)
    jobs_found_this_variation: Mapped[int] = mapped_column(Integer, default=0)
    jobs_collected: Mapped[int] = mapped_column(Integer, default=0)
    easy_apply_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_removed: Mapped[int] = mapped_column(Integer, default=0)
    latest_status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    resume: Mapped[ResumeProfile] = relationship(back_populates="search_batches")
    jobs: Mapped[list["JobOpportunity"]] = relationship(back_populates="search_batch")


class JobOpportunity(Base):
    __tablename__ = "job_opportunities"
    __table_args__ = (
        UniqueConstraint(
            "job_url",
            "resume_id",
            "search_batch_id",
            name="uq_job_url_resume_batch",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resume_profiles.id"), index=True)
    search_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_search_batches.id"),
        nullable=True,
        index=True,
    )
    search_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    portal: Mapped[str] = mapped_column(String(50), default="linkedin", index=True)
    company: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str] = mapped_column(Text, default="")
    easy_apply: Mapped[bool] = mapped_column(default=False)
    match_score: Mapped[float] = mapped_column(Float, default=0)
    match_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    complexity_score: Mapped[float] = mapped_column(Float, default=50)
    priority_score: Mapped[float] = mapped_column(Float, default=0, index=True)
    complexity_level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    estimated_steps: Mapped[int] = mapped_column(Integer, default=1)
    screening_question_count: Mapped[int] = mapped_column(Integer, default=0)
    required_fields: Mapped[int] = mapped_column(Integer, default=0)
    employment_history_required: Mapped[bool] = mapped_column(Boolean, default=False)
    application_friction_score: Mapped[float] = mapped_column(Float, default=50)
    completion_likelihood_score: Mapped[float] = mapped_column(Float, default=50)
    unsupported_field_count: Mapped[int] = mapped_column(Integer, default=0)
    external_form_risk: Mapped[float] = mapped_column(Float, default=0)
    manual_review_risk: Mapped[float] = mapped_column(Float, default=0)
    form_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    friction_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    recommended_apply_priority: Mapped[str | None] = mapped_column(String(30), nullable=True)
    apply_priority_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    freshness_score: Mapped[float] = mapped_column(Float, default=50)
    search_query_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    role_detected: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expanded_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    search_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_priority: Mapped[str | None] = mapped_column(String(50), nullable=True)
    priority_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_description_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    matched_skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    missing_skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    semantic_match_score: Mapped[float] = mapped_column(Float, default=0)
    overall_match_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_skills_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_skills_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    strength_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    weakness_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_cover_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_improvement_suggestions: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    technical_match_score: Mapped[float] = mapped_column(Float, default=0)
    industry_match_score: Mapped[float] = mapped_column(Float, default=0)
    confidence_score: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        SqlEnum(ApplicationStatus), default=ApplicationStatus.MATCHED, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    resume: Mapped[ResumeProfile] = relationship(back_populates="jobs")
    search_batch: Mapped[JobSearchBatch | None] = relationship(back_populates="jobs")
    applications: Mapped[list["ApplicationRecord"]] = relationship(back_populates="job")


class ApplicationRecord(Base):
    __tablename__ = "application_records"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job_opportunities.id"), index=True)
    portal: Mapped[str] = mapped_column(String(50), default="linkedin", index=True)
    status: Mapped[ApplicationStatus] = mapped_column(SqlEnum(ApplicationStatus), index=True)
    application_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    attempt_count: Mapped[int] = mapped_column(default=1)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    current_step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    resume_filename_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resume_upload_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resume_upload_evidence_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reasoning_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reasoning_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[str | None] = mapped_column(String(50), nullable=True)
    evidence_sources: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_log: Mapped[list[dict]] = mapped_column(JSONB, default=list)

    job: Mapped[JobOpportunity] = relationship(back_populates="applications")


class ApplyTopBatch(Base):
    __tablename__ = "apply_top_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resume_profiles.id"), index=True)
    search_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    portal: Mapped[str] = mapped_column(String(50), default="linkedin", index=True)
    status: Mapped[str] = mapped_column(String(50), default="queued", index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    screening_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    limit: Mapped[int] = mapped_column("application_limit", Integer, default=10)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    current_job_id: Mapped[int | None] = mapped_column(ForeignKey("job_opportunities.id"), nullable=True)
    current_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_review_count: Mapped[int] = mapped_column(Integer, default=0)
    timed_out_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    job_results: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def stopped_count(self) -> int:
        """How many jobs the user's Stop Automation ended -- the interrupted one plus every
        job that never started.

        Derived from ``job_results`` rather than stored in its own column: that JSONB list is
        already the authoritative per-job record, so counting it needs no schema change and no
        migration, and a batch written before Stop Automation existed simply reports 0."""
        from app.services.automation_control import JOB_RESULT_STOPPED

        return sum(1 for item in (self.job_results or []) if item.get("status") == JOB_RESULT_STOPPED)


class PortalRuntimeState(Base):
    __tablename__ = "portal_runtime_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    active_portal: Mapped[str] = mapped_column(String(50), default="linkedin")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
