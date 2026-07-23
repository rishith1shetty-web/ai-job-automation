from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import ApplicationStatus


class ResumeProfileOut(BaseModel):
    id: int
    portal: str = "linkedin"
    filename: str
    skills: list[str]
    experience: list[dict]
    education: list[dict]
    job_titles: list[str]
    keywords: list[str]
    technologies: list[str]
    domain_expertise: list[str]
    detected_role: str | None
    role_suggestions: list[dict]
    generated_search_keywords: list[str]
    generated_search_query: str | None
    role_confidence: float
    domain_profile: dict = {}
    role_profile: dict = {}
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobOut(BaseModel):
    id: int
    resume_id: int
    search_batch_id: str | None
    search_timestamp: datetime | None
    portal: str = "linkedin"
    title: str
    company: str
    location: str | None
    job_url: str
    easy_apply: bool
    match_score: float
    match_rationale: str | None
    complexity_score: float = 50
    priority_score: float = 0
    complexity_level: str | None = None
    estimated_steps: int = 1
    screening_question_count: int = 0
    required_fields: int = 0
    employment_history_required: bool = False
    application_friction_score: float = 50
    completion_likelihood_score: float = 50
    unsupported_field_count: int = 0
    external_form_risk: float = 0
    manual_review_risk: float = 0
    form_provider: str | None = None
    friction_reasons: list[str] = []
    recommended_apply_priority: str | None = None
    apply_priority_reason: str | None = None
    freshness_score: float = 50
    search_query_used: str | None = None
    role_detected: str | None = None
    expanded_role: str | None = None
    search_role: str | None = None
    role_priority: str | None = None
    priority_reason: str | None = None
    match_reason: str | None = None
    job_description_metadata: dict = {}
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    semantic_match_score: float = 0
    overall_match_summary: str | None = None
    matched_skills_summary: str | None = None
    missing_skills_summary: str | None = None
    strength_summary: str | None = None
    weakness_summary: str | None = None
    recommendation_summary: str | None = None
    generated_cover_letter: str | None = None
    resume_improvement_suggestions: list[dict] = []
    technical_match_score: float = 0
    industry_match_score: float = 0
    confidence_score: str | None = None
    status: ApplicationStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SearchBatchOut(BaseModel):
    id: str
    resume_id: int
    search_query: str
    location: str
    portal: str = "linkedin"
    status: str = "completed"
    requested_limit: int = 50
    current_search_variation: str | None = None
    current_variation_index: int = 0
    total_variations: int = 0
    jobs_found_this_variation: int = 0
    jobs_collected: int = 0
    easy_apply_count: int = 0
    duplicates_removed: int = 0
    latest_status_message: str | None = None
    error_message: str | None = None
    debug_path: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    job_count: int = 0
    updated_at: datetime | None = None


class CandidateProfileBase(BaseModel):
    portal: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    mobile_phone: str | None = None
    phone_country_code: str | None = None
    current_location: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    github_url: str | None = None
    address: str | None = None
    current_job_title: str | None = None
    professional_headline: str | None = None
    current_company: str | None = None
    previous_companies: list[str] = []
    certifications: list[str] = []
    professional_links: dict = {}
    field_sources: dict = {}
    missing_required_after_apply: list[str] = []


class CandidateProfileIn(CandidateProfileBase):
    pass


class CandidateProfileOut(CandidateProfileBase):
    id: int
    portal: str = "linkedin"
    is_default: bool
    missing_required_fields: list[str]
    is_complete: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CandidateEmploymentHistoryBase(BaseModel):
    resume_id: int | None = None
    display_order: int = 0
    job_title: str | None = None
    company_name: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    employment_type: str | None = None
    start_month: str | None = None
    start_year: int | None = None
    end_month: str | None = None
    end_year: int | None = None
    currently_work_here: bool = False
    description: str | None = None
    skills_used: list[str] = []
    technologies_used: list[str] = []
    duration_months: int | None = None
    confidence: float = 0
    source: str = "Resume Experience"


class CandidateEmploymentHistoryIn(CandidateEmploymentHistoryBase):
    pass


class CandidateEmploymentHistoryOut(CandidateEmploymentHistoryBase):
    id: int
    candidate_profile_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApplicationRecordOut(BaseModel):
    id: int
    job_id: int
    portal: str = "linkedin"
    status: ApplicationStatus
    application_date: datetime
    attempt_count: int
    dry_run: bool
    current_step: str | None
    evidence_path: str | None
    resume_filename_used: str | None = None
    resume_upload_status: str | None = None
    resume_upload_evidence_path: str | None = None
    reasoning_type: str | None = None
    reasoning_summary: str | None = None
    confidence_score: str | None = None
    evidence_sources: list[dict] = []
    error_message: str | None
    run_log: list[dict] = []

    model_config = ConfigDict(from_attributes=True)


class ApplicationHistoryOut(ApplicationRecordOut):
    job_title: str
    company: str
    job_url: str
    job_portal: str = "linkedin"


class ApplyTopBatchOut(BaseModel):
    id: str
    resume_id: int
    search_batch_id: str | None
    portal: str = "linkedin"
    status: str
    dry_run: bool
    screening_mode: str | None
    limit: int
    total_count: int
    current_index: int
    current_job_id: int | None
    current_title: str | None
    current_company: str | None
    completed_count: int
    skipped_count: int
    failed_count: int
    needs_review_count: int
    timed_out_count: int
    latest_status_message: str | None
    error_message: str | None
    debug_path: str | None
    job_results: list[dict]
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SearchRequest(BaseModel):
    resume_id: int
    location: str = "United States"
    keywords: str | None = None
    limit: int = 100
    portal: str | None = None


class ApplyRequest(BaseModel):
    resume_id: int
    limit: int = 10
    dry_run: bool | None = None
    search_batch_id: str | None = None
    screening_mode: str | None = None
    portal: str | None = None


class ApplySelectedRequest(BaseModel):
    resume_id: int
    job_id: int
    dry_run: bool | None = None
    screening_mode: str | None = None


class ScreeningAutomationSettingIn(BaseModel):
    mode: str = "safe"
    notice_period_default: str | None = None


class ScreeningAutomationSettingOut(ScreeningAutomationSettingIn):
    id: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ScreeningKnowledgeAnswerIn(BaseModel):
    question_text: str
    category: str = "general"
    answer: str
    source: str = "Manual"
    confidence: str = "High"
    currency: str | None = None
    evidence: dict = {}
    resume_id: int | None = None


class ScreeningKnowledgeAnswerOut(ScreeningKnowledgeAnswerIn):
    id: int
    candidate_profile_id: int | None
    normalized_question: str
    use_count: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
