import json
import logging
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db, init_db
from app.logging_config import configure_logging
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    CandidateProfile,
    CandidateEmploymentHistory,
    FormFieldLearning,
    GeneralizedFormFieldMetadata,
    JobOpportunity,
    JobSearchBatch,
    ResumeProfile,
    ScreeningAutomationSetting,
    ScreeningKnowledgeAnswer,
)
from app.schemas import (
    ApplicationHistoryOut,
    ApplicationRecordOut,
    ApplyRequest,
    ApplySelectedRequest,
    CandidateProfileIn,
    CandidateProfileOut,
    CandidateEmploymentHistoryIn,
    CandidateEmploymentHistoryOut,
    JobOut,
    ResumeProfileOut,
    SearchBatchOut,
    SearchRequest,
    ScreeningAutomationSettingIn,
    ScreeningAutomationSettingOut,
    ScreeningKnowledgeAnswerIn,
    ScreeningKnowledgeAnswerOut,
)
from app.services.linkedin import AutomationResult, LinkedInAutomation
from app.services.find_matches import FindMatchesEngine
from app.services.form_learning import FormLearningEngine
from app.services.generalized_form_engine import GeneralizedFieldInput, GeneralizedFormEngine
from app.services.matcher import MatchScorer
from app.services.performance import PerformanceRecorder, latest_performance_summary
from app.services.reasoning import AIReasoningEngine
from app.services.resume_intelligence import ResumeIntelligenceEngine
from app.services.resume_parser import ResumeParser

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()
app = FastAPI(title="LinkedIn AI Job Matcher", version="0.1.0")
SEARCH_PLAN_CACHE: dict[tuple, tuple[list, dict]] = {}


def resolve_search_query(resume: ResumeProfile, manual_keywords: str | None) -> str:
    manual_override = (manual_keywords or "").strip()
    if manual_override:
        return manual_override
    if resume.generated_search_query:
        return resume.generated_search_query
    search_terms = resume.generated_search_keywords[:5] or [resume.detected_role or "Software Engineer"]
    return " OR ".join(search_terms)


def latest_search_batch(db: Session, resume_id: int) -> JobSearchBatch | None:
    return db.scalar(
        select(JobSearchBatch)
        .where(JobSearchBatch.resume_id == resume_id)
        .order_by(desc(JobSearchBatch.created_at))
        .limit(1)
    )


def default_candidate_profile(db: Session) -> CandidateProfile | None:
    return db.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.is_default.is_(True))
        .order_by(desc(CandidateProfile.updated_at), desc(CandidateProfile.created_at))
        .limit(1)
    )


def normalized_screening_question(question: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    replacements = {
        "will you require visa sponsorship": "work sponsorship",
        "do you require sponsorship": "work sponsorship",
        "do you need employment sponsorship": "work sponsorship",
        "expected ctc": "expected salary",
        "desired salary": "expected salary",
        "compensation expectation": "expected salary",
    }
    return replacements.get(normalized, normalized[:512])


def default_screening_setting(db: Session) -> ScreeningAutomationSetting:
    setting = db.scalar(select(ScreeningAutomationSetting).order_by(ScreeningAutomationSetting.id).limit(1))
    if not setting:
        setting = ScreeningAutomationSetting(mode="safe", notice_period_default="30 days")
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting


def screening_answers_for_candidate(
    db: Session,
    candidate_profile: CandidateProfile | None,
) -> list[dict]:
    if candidate_profile is None:
        return []
    answers = list(
        db.scalars(
            select(ScreeningKnowledgeAnswer)
            .where(ScreeningKnowledgeAnswer.candidate_profile_id == candidate_profile.id)
            .order_by(desc(ScreeningKnowledgeAnswer.updated_at))
        ).all()
    )
    return [
        {
            "id": answer.id,
            "question_text": answer.question_text,
            "normalized_question": answer.normalized_question,
            "category": answer.category,
            "answer": answer.answer,
            "source": answer.source,
            "confidence": answer.confidence,
            "currency": answer.currency,
            "evidence": answer.evidence,
        }
        for answer in answers
    ]


PROFILE_FIELDS = {
    "first_name",
    "last_name",
    "email",
    "mobile_phone",
    "phone_country_code",
    "current_location",
    "linkedin_url",
    "portfolio_url",
    "github_url",
    "address",
    "current_job_title",
    "professional_headline",
    "current_company",
}


def candidate_profile_payload(profile: CandidateProfile) -> dict:
    return {
        field: getattr(profile, field)
        for field in PROFILE_FIELDS
    } | {
        "previous_companies": profile.previous_companies,
        "certifications": profile.certifications,
        "professional_links": profile.professional_links,
        "field_sources": profile.field_sources,
        "missing_required_after_apply": profile.missing_required_after_apply,
    }


def merge_candidate_profile(
    profile: CandidateProfile,
    extracted: dict,
    *,
    overwrite_manual: bool = False,
) -> CandidateProfile:
    field_sources = dict(profile.field_sources or {})
    for field, metadata in (extracted.get("fields") or {}).items():
        if field not in PROFILE_FIELDS:
            continue
        value = str(metadata.get("value") or "").strip()
        if not value:
            continue
        source = metadata.get("source") or "Unknown"
        confidence = float(metadata.get("confidence") or 0)
        existing_value = getattr(profile, field, None)
        existing_source = field_sources.get(field, {})
        existing_confidence = float(existing_source.get("confidence") or 0)
        existing_is_manual = existing_source.get("source") == "Manual"
        should_update = (
            not existing_value
            or confidence > existing_confidence
            or (overwrite_manual and existing_is_manual)
        )
        if should_update and not (existing_is_manual and not overwrite_manual and existing_value):
            setattr(profile, field, value)
            field_sources[field] = {
                "source": source,
                "confidence": confidence,
                "updated_at": datetime.utcnow().isoformat(),
            }

    links = dict(profile.professional_links or {})
    for name, metadata in (extracted.get("professional_links") or {}).items():
        existing = links.get(name, {})
        if float(metadata.get("confidence") or 0) >= float(existing.get("confidence") or 0):
            links[name] = metadata
    profile.professional_links = links
    profile.previous_companies = list(
        dict.fromkeys((profile.previous_companies or []) + (extracted.get("previous_companies") or []))
    )[:20]
    profile.certifications = list(
        dict.fromkeys((profile.certifications or []) + (extracted.get("certifications") or []))
    )[:30]
    profile.field_sources = field_sources
    return profile


def write_profile_debug(
    stage: str,
    extracted_resume: dict | None = None,
    extracted_linkedin: dict | None = None,
    merged_profile: CandidateProfile | None = None,
) -> Path:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    debug_dir = settings.profile_autofill_debug_dir / timestamp / stage
    debug_dir.mkdir(parents=True, exist_ok=True)
    if extracted_resume is not None:
        (debug_dir / "extracted_from_resume.json").write_text(
            json.dumps(extracted_resume, indent=2),
            encoding="utf-8",
        )
    if extracted_linkedin is not None:
        (debug_dir / "extracted_from_linkedin_profile.json").write_text(
            json.dumps(extracted_linkedin, indent=2),
            encoding="utf-8",
        )
    if merged_profile is not None:
        (debug_dir / "merged_candidate_profile.json").write_text(
            json.dumps(candidate_profile_payload(merged_profile), indent=2, default=str),
            encoding="utf-8",
        )
    return debug_dir


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/resumes", response_model=ResumeProfileOut)
def upload_resume(upload: UploadFile = File(...), db: Session = Depends(get_db)) -> ResumeProfile:
    parser = ResumeParser(settings)
    stored_path = parser.save_upload(upload)
    raw_text = parser.extract_text(stored_path)
    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from resume.")
    profile = parser.parse(raw_text)
    resume = ResumeProfile(
        filename=upload.filename or Path(stored_path).name,
        stored_path=str(stored_path.resolve()),
        raw_text=raw_text,
        skills=profile["skills"],
        experience=profile["experience"],
        education=profile["education"],
        job_titles=profile["job_titles"],
        keywords=profile["keywords"],
        technologies=profile["technologies"],
        domain_expertise=profile["domain_expertise"],
        detected_role=profile["detected_role"],
        role_suggestions=profile["role_suggestions"],
        generated_search_keywords=profile["generated_search_keywords"],
        generated_search_query=profile["generated_search_query"],
        role_confidence=profile["role_confidence"],
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    candidate = default_candidate_profile(db) or CandidateProfile(is_default=True)
    db.add(candidate)
    resume_profile = parser.extract_candidate_profile(raw_text, parser.extract_hyperlinks(stored_path))
    merge_candidate_profile(candidate, resume_profile)
    linkedin_profile = None
    if LinkedInAutomation(settings).has_authenticated_session():
        try:
            linkedin_profile = LinkedInAutomation(settings).extract_logged_in_profile()
            merge_candidate_profile(candidate, linkedin_profile)
        except Exception:
            logger.exception("LinkedIn profile fallback extraction failed.")
    db.commit()
    db.refresh(candidate)
    employment_records = parser.extract_employment_history(raw_text, candidate.current_location)
    save_extracted_employment_history(db, candidate, resume, employment_records)
    write_profile_debug(
        f"resume_{resume.id}",
        extracted_resume=resume_profile,
        extracted_linkedin=linkedin_profile,
        merged_profile=candidate,
    )
    return resume


@app.get("/resumes/latest", response_model=ResumeProfileOut)
def latest_resume(db: Session = Depends(get_db)) -> ResumeProfile:
    resume = db.scalar(select(ResumeProfile).order_by(desc(ResumeProfile.created_at)).limit(1))
    if not resume:
        raise HTTPException(status_code=404, detail="No resume has been analyzed.")
    return resume


@app.get("/resumes/{resume_id}", response_model=ResumeProfileOut)
def get_resume(resume_id: int, db: Session = Depends(get_db)) -> ResumeProfile:
    resume = db.get(ResumeProfile, resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")
    return resume


@app.post("/linkedin/connect")
def connect_linkedin() -> dict:
    automation = LinkedInAutomation(settings)
    automation.connect_account()
    return {"status": "connected", "storage_state": str(settings.linkedin_storage_state)}


@app.get("/linkedin/status")
def linkedin_status() -> dict:
    return {"connected": LinkedInAutomation(settings).has_authenticated_session()}


@app.post("/linkedin/disconnect")
def disconnect_linkedin() -> dict:
    LinkedInAutomation(settings).disconnect_account()
    return {"status": "disconnected"}


@app.get("/candidate-profile", response_model=CandidateProfileOut)
def get_candidate_profile(db: Session = Depends(get_db)) -> CandidateProfile:
    profile = default_candidate_profile(db)
    if not profile:
        profile = CandidateProfile(is_default=True)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def save_extracted_employment_history(
    db: Session,
    candidate: CandidateProfile,
    resume: ResumeProfile,
    records: list[dict],
) -> None:
    existing = db.scalars(
        select(CandidateEmploymentHistory)
        .where(CandidateEmploymentHistory.candidate_profile_id == candidate.id)
        .where(CandidateEmploymentHistory.resume_id == resume.id)
    ).first()
    if existing:
        return
    for order, payload in enumerate(records):
        record = CandidateEmploymentHistory(
            candidate_profile_id=candidate.id,
            resume_id=resume.id,
            display_order=payload.get("display_order", order),
            job_title=payload.get("job_title"),
            company_name=payload.get("company_name"),
            city=payload.get("city"),
            state=payload.get("state"),
            country=payload.get("country"),
            employment_type=payload.get("employment_type"),
            start_month=payload.get("start_month"),
            start_year=payload.get("start_year"),
            end_month=payload.get("end_month"),
            end_year=payload.get("end_year"),
            currently_work_here=bool(payload.get("currently_work_here")),
            description=payload.get("description"),
            skills_used=payload.get("skills_used") or [],
            technologies_used=payload.get("technologies_used") or [],
            duration_months=payload.get("duration_months"),
            confidence=float(payload.get("confidence") or 0),
            source=payload.get("source") or "Resume Experience",
        )
        db.add(record)
    db.commit()


def ensure_employment_history_for_resume(
    db: Session,
    candidate: CandidateProfile | None,
    resume: ResumeProfile,
) -> list[CandidateEmploymentHistory]:
    if candidate is None:
        return []
    existing = list(
        db.scalars(
            select(CandidateEmploymentHistory)
            .where(CandidateEmploymentHistory.candidate_profile_id == candidate.id)
            .where(CandidateEmploymentHistory.resume_id == resume.id)
            .order_by(CandidateEmploymentHistory.display_order, CandidateEmploymentHistory.created_at)
        ).all()
    )
    if existing:
        return existing
    parser = ResumeParser(settings)
    records = parser.extract_employment_history(resume.raw_text or "", candidate.current_location)
    save_extracted_employment_history(db, candidate, resume, records)
    return list(
        db.scalars(
            select(CandidateEmploymentHistory)
            .where(CandidateEmploymentHistory.candidate_profile_id == candidate.id)
            .where(CandidateEmploymentHistory.resume_id == resume.id)
            .order_by(CandidateEmploymentHistory.display_order, CandidateEmploymentHistory.created_at)
        ).all()
    )


def employment_history_payload(records: list[CandidateEmploymentHistory]) -> list[dict]:
    return [
        {
            "id": record.id,
            "candidate_profile_id": record.candidate_profile_id,
            "resume_id": record.resume_id,
            "display_order": record.display_order,
            "job_title": record.job_title,
            "company_name": record.company_name,
            "city": record.city,
            "state": record.state,
            "country": record.country,
            "employment_type": record.employment_type,
            "start_month": record.start_month,
            "start_year": record.start_year,
            "end_month": record.end_month,
            "end_year": record.end_year,
            "currently_work_here": record.currently_work_here,
            "description": record.description,
            "skills_used": record.skills_used or [],
            "technologies_used": record.technologies_used or [],
            "duration_months": record.duration_months,
            "confidence": record.confidence,
            "source": record.source,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        for record in records
    ]


@app.get("/employment-history", response_model=list[CandidateEmploymentHistoryOut])
def list_employment_history(
    resume_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[CandidateEmploymentHistory]:
    candidate = default_candidate_profile(db)
    if candidate is None:
        return []
    if resume_id:
        resume = db.get(ResumeProfile, resume_id)
        if resume:
            ensure_employment_history_for_resume(db, candidate, resume)
    statement = (
        select(CandidateEmploymentHistory)
        .where(CandidateEmploymentHistory.candidate_profile_id == candidate.id)
        .order_by(CandidateEmploymentHistory.display_order, CandidateEmploymentHistory.created_at)
    )
    if resume_id:
        statement = statement.where(CandidateEmploymentHistory.resume_id == resume_id)
    return list(db.scalars(statement).all())


@app.post("/employment-history", response_model=CandidateEmploymentHistoryOut)
def add_employment_history(
    payload: CandidateEmploymentHistoryIn,
    db: Session = Depends(get_db),
) -> CandidateEmploymentHistory:
    candidate = default_candidate_profile(db)
    if candidate is None:
        candidate = CandidateProfile(is_default=True)
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
    record = CandidateEmploymentHistory(candidate_profile_id=candidate.id)
    update_employment_record(record, payload)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.put("/employment-history/{employment_id}", response_model=CandidateEmploymentHistoryOut)
def update_employment_history(
    employment_id: int,
    payload: CandidateEmploymentHistoryIn,
    db: Session = Depends(get_db),
) -> CandidateEmploymentHistory:
    candidate = default_candidate_profile(db)
    record = db.get(CandidateEmploymentHistory, employment_id)
    if not record or not candidate or record.candidate_profile_id != candidate.id:
        raise HTTPException(status_code=404, detail="Employment history record not found.")
    update_employment_record(record, payload)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.delete("/employment-history/{employment_id}")
def delete_employment_history(employment_id: int, db: Session = Depends(get_db)) -> dict:
    candidate = default_candidate_profile(db)
    record = db.get(CandidateEmploymentHistory, employment_id)
    if not record or not candidate or record.candidate_profile_id != candidate.id:
        raise HTTPException(status_code=404, detail="Employment history record not found.")
    db.delete(record)
    db.commit()
    return {"status": "deleted"}


def update_employment_record(
    record: CandidateEmploymentHistory,
    payload: CandidateEmploymentHistoryIn,
) -> None:
    for field, value in payload.model_dump().items():
        if isinstance(value, str):
            value = value.strip() or None
        if field in {"skills_used", "technologies_used"}:
            value = value or []
        setattr(record, field, value)


@app.put("/candidate-profile", response_model=CandidateProfileOut)
def save_candidate_profile(
    payload: CandidateProfileIn,
    db: Session = Depends(get_db),
) -> CandidateProfile:
    profile = default_candidate_profile(db)
    if not profile:
        profile = CandidateProfile(is_default=True)
        db.add(profile)
    for field, value in payload.model_dump().items():
        if field in {"field_sources", "missing_required_after_apply"}:
            continue
        if isinstance(value, str):
            value = value.strip() or None
        if field in PROFILE_FIELDS:
            setattr(profile, field, value)
            if value:
                sources = dict(profile.field_sources or {})
                sources[field] = {
                    "source": "Manual",
                    "confidence": 100,
                    "updated_at": datetime.utcnow().isoformat(),
                }
                profile.field_sources = sources
        elif field in {"previous_companies", "certifications"}:
            setattr(profile, field, value or [])
        elif field == "professional_links":
            profile.professional_links = value or {}
    profile.is_default = True
    db.commit()
    db.refresh(profile)
    write_profile_debug("manual_save", merged_profile=profile)
    return profile


@app.get("/screening/settings", response_model=ScreeningAutomationSettingOut)
def get_screening_settings(db: Session = Depends(get_db)) -> ScreeningAutomationSetting:
    return default_screening_setting(db)


@app.put("/screening/settings", response_model=ScreeningAutomationSettingOut)
def save_screening_settings(
    payload: ScreeningAutomationSettingIn,
    db: Session = Depends(get_db),
) -> ScreeningAutomationSetting:
    setting = default_screening_setting(db)
    mode = payload.mode.lower().strip()
    if mode not in {"safe", "aggressive"}:
        raise HTTPException(status_code=400, detail="Screening mode must be safe or aggressive.")
    setting.mode = mode
    setting.notice_period_default = (payload.notice_period_default or "").strip() or None
    db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting


@app.get("/screening/knowledge", response_model=list[ScreeningKnowledgeAnswerOut])
def list_screening_knowledge(db: Session = Depends(get_db)) -> list[ScreeningKnowledgeAnswer]:
    candidate = default_candidate_profile(db)
    if candidate is None:
        return []
    return list(
        db.scalars(
            select(ScreeningKnowledgeAnswer)
            .where(ScreeningKnowledgeAnswer.candidate_profile_id == candidate.id)
            .order_by(desc(ScreeningKnowledgeAnswer.updated_at))
        ).all()
    )


@app.post("/screening/knowledge", response_model=ScreeningKnowledgeAnswerOut)
def save_screening_knowledge(
    payload: ScreeningKnowledgeAnswerIn,
    db: Session = Depends(get_db),
) -> ScreeningKnowledgeAnswer:
    candidate = default_candidate_profile(db)
    if candidate is None:
        candidate = CandidateProfile(is_default=True)
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
    normalized = normalized_screening_question(payload.question_text)
    existing = db.scalar(
        select(ScreeningKnowledgeAnswer)
        .where(ScreeningKnowledgeAnswer.candidate_profile_id == candidate.id)
        .where(ScreeningKnowledgeAnswer.normalized_question == normalized)
        .limit(1)
    )
    answer = existing or ScreeningKnowledgeAnswer(
        candidate_profile_id=candidate.id,
        normalized_question=normalized,
    )
    answer.resume_id = payload.resume_id
    answer.question_text = payload.question_text.strip()
    answer.category = payload.category.strip() or "general"
    answer.answer = payload.answer.strip()
    answer.source = payload.source.strip() or "Manual"
    answer.confidence = payload.confidence.strip() or "High"
    answer.currency = (payload.currency or "").strip() or None
    answer.evidence = payload.evidence or {}
    db.add(answer)
    db.commit()
    db.refresh(answer)
    return answer


@app.delete("/screening/knowledge/{answer_id}")
def delete_screening_knowledge(answer_id: int, db: Session = Depends(get_db)) -> dict:
    candidate = default_candidate_profile(db)
    answer = db.get(ScreeningKnowledgeAnswer, answer_id)
    if not answer or not candidate or answer.candidate_profile_id != candidate.id:
        raise HTTPException(status_code=404, detail="Saved screening answer not found.")
    db.delete(answer)
    db.commit()
    return {"status": "deleted"}


@app.post("/jobs/search", response_model=list[JobOut])
def search_jobs(request: SearchRequest, db: Session = Depends(get_db)) -> list[JobOpportunity]:
    recorder = PerformanceRecorder(
        settings,
        "find_matches",
        {"resume_id": request.resume_id, "location": request.location, "limit": request.limit},
    )
    try:
        with recorder.stage("resume_loading"):
            resume = db.get(ResumeProfile, request.resume_id)
        if not resume:
            raise HTTPException(status_code=404, detail="Resume not found.")
        with recorder.stage("linkedin_session_validation"):
            if not settings.linkedin_storage_state.exists():
                raise HTTPException(status_code=409, detail="Connect LinkedIn before searching for jobs.")

        engine = FindMatchesEngine(settings)
        debug_dir = engine.create_debug_dir()
        cache_key = (
            resume.id,
            resume.created_at.isoformat() if resume.created_at else "",
            request.location,
            (request.keywords or "").strip(),
            resume.generated_search_query or "",
        )
        if cache_key in SEARCH_PLAN_CACHE:
            search_variations, search_plan = SEARCH_PLAN_CACHE[cache_key]
            recorder.cache("search_plan", True, resume_id=resume.id)
        else:
            with recorder.stage("role_skill_location_expansion"):
                search_variations, search_plan = engine.build_search_plan(resume, request.location, request.keywords)
            SEARCH_PLAN_CACHE[cache_key] = (search_variations, search_plan)
            recorder.cache("search_plan", False, resume_id=resume.id)
        search_query = search_plan["display_query"] or resolve_search_query(resume, request.keywords)
        search_timestamp = datetime.utcnow()
        search_batch = JobSearchBatch(
            id=str(uuid4()),
            resume_id=resume.id,
            search_query=search_query,
            location=request.location,
            created_at=search_timestamp,
        )
        db.add(search_batch)
        db.commit()

        linkedin = LinkedInAutomation(settings)
        engine.write_debug(debug_dir, "search_queries.json", search_plan["queries"])
        engine.write_debug(debug_dir, "expanded_roles.json", search_plan["expanded_roles"])
        engine.write_debug(debug_dir, "expanded_skills.json", search_plan["expanded_skills"])
        engine.write_debug(debug_dir, "location_expansion.json", search_plan["locations"])

        target_count = engine.target_count(request.limit)
        jobs: list[JobOpportunity] = []
        seen_keys: set[str] = set()
        duplicate_jobs: list[dict] = []
        jobs_found: list[dict] = []
        with recorder.stage("duplicate_key_loading"):
            previous_jobs = list(
                db.scalars(select(JobOpportunity).where(JobOpportunity.resume_id == resume.id)).all()
            )
            previous_keys = {
                key
                for previous_job in previous_jobs
                for key in engine.duplicate_key(
                    {
                        "job_url": previous_job.job_url,
                        "title": previous_job.title,
                        "company": previous_job.company,
                        "location": previous_job.location,
                    }
                )
                if key
            }
        selected_variations = search_variations[:4]
        if hasattr(linkedin, "search_jobs_many"):
            with recorder.stage("linkedin_find_matches_page_loading", query_count=len(selected_variations)):
                variation_results = linkedin.search_jobs_many(
                    resume,
                    selected_variations,
                    5,
                    collect_card_diagnostics=False,
                )
        else:
            variation_results = []
            for variation in selected_variations:
                with recorder.stage("linkedin_find_matches_page_loading", query=variation.query, location=variation.location):
                    try:
                        raw_jobs = linkedin.search_jobs(
                            resume,
                            variation.location,
                            variation.query,
                            5,
                            collect_card_diagnostics=False,
                        )
                    except TypeError:
                        raw_jobs = linkedin.search_jobs(resume, variation.location, variation.query, 5)
                variation_results.append((variation, raw_jobs))
        for variation, raw_jobs in variation_results:
            for raw in raw_jobs:
                keys = [key for key in engine.duplicate_key(raw) if key]
                duplicate_reason = None
                if any(key in seen_keys for key in keys):
                    duplicate_reason = "Duplicate in current search batch"
                elif any(key in previous_keys for key in keys):
                    duplicate_reason = "Already present in previous batch for this resume"
                if duplicate_reason:
                    duplicate_jobs.append(
                        {
                            "title": raw.get("title"),
                            "company": raw.get("company"),
                            "job_url": raw.get("job_url"),
                            "reason": duplicate_reason,
                            "search_query_used": variation.query,
                        }
                    )
                    continue
                seen_keys.update(keys)
                enriched = engine.enrich_raw_job(raw, variation)
                job = JobOpportunity(
                    resume_id=resume.id,
                    search_batch_id=search_batch.id,
                    search_timestamp=search_timestamp,
                    **enriched,
                )
                db.add(job)
                jobs.append(job)
                jobs_found.append(
                    {
                        "title": job.title,
                        "company": job.company,
                        "job_url": job.job_url,
                        "easy_apply": job.easy_apply,
                        "search_query_used": job.search_query_used,
                        "location": job.location,
                    }
                )
                if len(jobs) >= target_count or sum(1 for item in jobs if item.easy_apply) >= target_count:
                    break
            engine.write_debug(debug_dir, "jobs_found.json", jobs_found)
            engine.write_debug(debug_dir, "duplicate_jobs.json", duplicate_jobs)
            if len(jobs) >= target_count or sum(1 for item in jobs if item.easy_apply) >= target_count:
                break

        scorer = MatchScorer(settings)
        with recorder.stage("match_complexity_priority_scoring", job_count=len(jobs)):
            ranked = engine.apply_priority_scores(scorer.score_jobs(resume, jobs))
        reasoning = AIReasoningEngine(settings)
        intelligence = ResumeIntelligenceEngine(settings, reasoning)
        with recorder.stage("job_description_reasoning", job_count=len(ranked)):
            reasoning_debug_dir = reasoning.create_debug_dir()
            intelligence_debug_dir = intelligence.create_debug_dir()
            reasoning_payload = []
            intelligence_payload = []
            for job in ranked:
                metadata = reasoning.analyze_job_description(job)
                comparison = reasoning.compare_resume_to_job(resume, job)
                job.job_description_metadata = metadata
                job.matched_skills = comparison["matched_skills"]
                job.missing_skills = comparison["missing_skills"]
                job.semantic_match_score = float(comparison["overall_semantic_match"])
                intelligence_result = intelligence.analyze_job(resume, job)
                intelligence.apply_to_job(job, intelligence_result)
                reasoning_payload.append(
                    {
                        "job_id": job.id,
                        "title": job.title,
                        "company": job.company,
                        "metadata": metadata,
                        "semantic_match": comparison,
                    }
                )
                intelligence_payload.append(
                    {
                        "job": {
                            "id": job.id,
                            "title": job.title,
                            "company": job.company,
                            "job_url": job.job_url,
                        },
                        "result": intelligence_result.__dict__,
                    }
                )
            engine.write_debug(reasoning_debug_dir, "job_description_analysis.json", reasoning_payload)
            engine.write_debug(reasoning_debug_dir, "semantic_matches.json", reasoning_payload)
            intelligence.write_debug(intelligence_debug_dir, intelligence_payload)
        for job in ranked:
            db.add(job)
        with recorder.stage("database_batch_write", job_count=len(ranked)):
            db.commit()
        engine.write_debug(debug_dir, "jobs_found.json", jobs_found)
        engine.write_debug(debug_dir, "duplicate_jobs.json", duplicate_jobs)
        engine.write_debug(
            debug_dir,
            "match_scores.json",
            [
                {
                    "title": job.title,
                    "company": job.company,
                    "match_score": job.match_score,
                    "match_reason": job.match_reason,
                    "semantic_match_score": job.semantic_match_score,
                    "matched_skills": job.matched_skills,
                    "missing_skills": job.missing_skills,
                    "overall_match_summary": job.overall_match_summary,
                    "technical_match_score": job.technical_match_score,
                    "industry_match_score": job.industry_match_score,
                    "confidence_score": job.confidence_score,
                }
                for job in ranked
            ],
        )
        engine.write_debug(
            debug_dir,
            "complexity_scores.json",
            [
                {
                    "title": job.title,
                    "company": job.company,
                    "complexity_score": job.complexity_score,
                    "complexity_level": job.complexity_level,
                    "estimated_steps": job.estimated_steps,
                    "screening_question_count": job.screening_question_count,
                    "required_fields": job.required_fields,
                    "employment_history_required": job.employment_history_required,
                }
                for job in ranked
            ],
        )
        engine.write_debug(
            debug_dir,
            "priority_queue.json",
            [
                {
                    "rank": index,
                    "title": job.title,
                    "company": job.company,
                    "priority_score": job.priority_score,
                    "priority_reason": job.priority_reason,
                    "search_query_used": job.search_query_used,
                }
                for index, job in enumerate(ranked, start=1)
            ],
        )
        return ranked[:target_count]
    finally:
        recorder.save()


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(
    resume_id: int | None = None,
    search_batch_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[JobOpportunity]:
    statement = select(JobOpportunity).order_by(
        desc(JobOpportunity.priority_score),
        desc(JobOpportunity.match_score),
        desc(JobOpportunity.easy_apply),
    )
    if resume_id:
        statement = statement.where(JobOpportunity.resume_id == resume_id)
        if search_batch_id is None:
            latest_batch = latest_search_batch(db, resume_id)
            if latest_batch:
                search_batch_id = latest_batch.id
    if search_batch_id:
        statement = statement.where(JobOpportunity.search_batch_id == search_batch_id)
    return list(db.scalars(statement).all())


@app.get("/jobs/search-batches", response_model=list[SearchBatchOut])
def list_search_batches(
    resume_id: int,
    db: Session = Depends(get_db),
) -> list[dict]:
    batches = list(
        db.scalars(
            select(JobSearchBatch)
            .where(JobSearchBatch.resume_id == resume_id)
            .order_by(desc(JobSearchBatch.created_at))
        ).all()
    )
    return [
        {
            "id": batch.id,
            "resume_id": batch.resume_id,
            "search_query": batch.search_query,
            "location": batch.location,
            "created_at": batch.created_at,
            "job_count": db.scalar(
                select(func.count(JobOpportunity.id)).where(
                    JobOpportunity.search_batch_id == batch.id
                )
            )
            or 0,
            "easy_apply_count": db.scalar(
                select(func.count(JobOpportunity.id)).where(
                    JobOpportunity.search_batch_id == batch.id,
                    JobOpportunity.easy_apply.is_(True),
                )
            )
            or 0,
        }
        for batch in batches
    ]


@app.post("/applications/apply-top", response_model=list[ApplicationRecordOut])
def apply_top_jobs(request: ApplyRequest, db: Session = Depends(get_db)) -> list[ApplicationRecord]:
    recorder = PerformanceRecorder(
        settings,
        "apply_top",
        {"resume_id": request.resume_id, "limit": request.limit, "dry_run": request.dry_run},
    )
    try:
        with recorder.stage("resume_loading"):
            resume = db.get(ResumeProfile, request.resume_id)
        if not resume:
            raise HTTPException(status_code=404, detail="Resume not found.")
        dry_run = settings.auto_apply_dry_run if request.dry_run is None else request.dry_run
        limit = min(request.limit, settings.max_applications_per_run)
        search_batch_id = request.search_batch_id
        with recorder.stage("job_queue_loading"):
            if search_batch_id:
                batch = db.get(JobSearchBatch, search_batch_id)
                if not batch or batch.resume_id != resume.id:
                    raise HTTPException(status_code=404, detail="Search batch not found for this resume.")
            else:
                batch = latest_search_batch(db, resume.id)
                search_batch_id = batch.id if batch else None
            job_statement = (
                select(JobOpportunity)
                .where(JobOpportunity.resume_id == resume.id)
                .where(JobOpportunity.status.in_([ApplicationStatus.MATCHED, ApplicationStatus.QUEUED]))
            )
            if search_batch_id:
                job_statement = job_statement.where(JobOpportunity.search_batch_id == search_batch_id)
            jobs = list(
                db.scalars(
                    job_statement.order_by(
                        desc(JobOpportunity.priority_score),
                        desc(JobOpportunity.match_score),
                        desc(JobOpportunity.easy_apply),
                    ).limit(limit)
                ).all()
            )
        automation = LinkedInAutomation(settings)
        records: list[ApplicationRecord] = []
        with recorder.stage("candidate_profile_loading"):
            candidate_profile = default_candidate_profile(db)
            screening_setting = default_screening_setting(db)
            screening_mode = (request.screening_mode or screening_setting.mode or "safe").lower()
            screening_context = {
                "mode": screening_mode,
                "notice_period_default": screening_setting.notice_period_default,
                "saved_answers": screening_answers_for_candidate(db, candidate_profile),
                "employment_history": employment_history_payload(
                    ensure_employment_history_for_resume(db, candidate_profile, resume)
                ),
            }
        for index, job in enumerate(jobs, start=1):
            with recorder.stage("easy_apply_application", job_id=job.id, index=index):
                records.append(
                    _process_application(
                        db,
                        automation,
                        job,
                        resume,
                        candidate_profile,
                        dry_run,
                        capture_evidence=dry_run,
                        screening_context=screening_context,
                    )
                )
        return records
    finally:
        recorder.save()


@app.post("/applications/apply-selected", response_model=ApplicationRecordOut)
def apply_selected_job(
    request: ApplySelectedRequest,
    db: Session = Depends(get_db),
) -> ApplicationRecord:
    recorder = PerformanceRecorder(
        settings,
        "apply_selected",
        {"resume_id": request.resume_id, "job_id": request.job_id, "dry_run": request.dry_run},
    )
    try:
        with recorder.stage("resume_loading"):
            resume = db.get(ResumeProfile, request.resume_id)
        if not resume:
            raise HTTPException(status_code=404, detail="Resume not found.")
        with recorder.stage("job_loading"):
            job = db.get(JobOpportunity, request.job_id)
        if not job or job.resume_id != resume.id:
            raise HTTPException(status_code=404, detail="Selected job was not found for this resume.")
        dry_run = settings.auto_apply_dry_run if request.dry_run is None else request.dry_run
        automation = LinkedInAutomation(settings)
        with recorder.stage("candidate_profile_loading"):
            screening_setting = default_screening_setting(db)
            screening_mode = (request.screening_mode or screening_setting.mode or "safe").lower()
            candidate_profile = default_candidate_profile(db)
            screening_context = {
                "mode": screening_mode,
                "notice_period_default": screening_setting.notice_period_default,
                "saved_answers": screening_answers_for_candidate(db, candidate_profile),
                "employment_history": employment_history_payload(
                    ensure_employment_history_for_resume(db, candidate_profile, resume)
                ),
            }
        with recorder.stage("easy_apply_application", job_id=job.id):
            return _process_application(
                db,
                automation,
                job,
                resume,
                candidate_profile,
                dry_run,
                capture_evidence=True,
                screening_context=screening_context,
            )
    finally:
        recorder.save()


def _process_application(
    db: Session,
    automation: LinkedInAutomation,
    job: JobOpportunity,
    resume: ResumeProfile,
    candidate_profile: CandidateProfile | None,
    dry_run: bool,
    capture_evidence: bool,
    screening_context: dict | None = None,
) -> ApplicationRecord:
    try:
        result = automation.apply_to_job(
            job,
            resume,
            candidate_profile,
            dry_run=dry_run,
            capture_evidence=capture_evidence,
            screening_context=screening_context,
        )
        job.status = ApplicationStatus.MATCHED if dry_run else result.status
        if candidate_profile is not None:
            candidate_profile.missing_required_after_apply = result.missing_contact_fields or []
            db.add(candidate_profile)
            _save_skill_experience_estimates(db, candidate_profile, resume, result)
        _save_form_learning_observations(db, job, result)
        record = ApplicationRecord(
            job_id=job.id,
            status=result.status,
            dry_run=dry_run,
            current_step=result.current_step,
            evidence_path=result.evidence_path,
            resume_filename_used=_resume_upload_field(result, "resume_original_filename"),
            resume_upload_status=_resume_upload_status(result),
            resume_upload_evidence_path=_resume_upload_field(result, "evidence_path"),
            **_reasoning_record_fields(result),
            error_message=result.message,
            run_log=result.log,
        )
    except Exception as exc:
        logger.exception("Application failed for job %s", job.id)
        job.status = ApplicationStatus.FAILED
        record = ApplicationRecord(
            job_id=job.id,
            status=ApplicationStatus.FAILED,
            dry_run=dry_run,
            current_step="automation_failed",
            error_message=str(exc),
            run_log=[],
        )
    db.add(job)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _save_skill_experience_estimates(
    db: Session,
    candidate_profile: CandidateProfile,
    resume: ResumeProfile,
    result: AutomationResult,
) -> None:
    for event in result.screening_answers or []:
        if event.get("category") != "skill_years" or not event.get("selected_answer"):
            continue
        evidence = event.get("evidence") or {}
        skill = evidence.get("normalized_skill")
        if not skill:
            continue
        normalized = normalized_screening_question(f"{skill} years experience")
        existing = db.scalar(
            select(ScreeningKnowledgeAnswer)
            .where(ScreeningKnowledgeAnswer.candidate_profile_id == candidate_profile.id)
            .where(ScreeningKnowledgeAnswer.normalized_question == normalized)
            .limit(1)
        )
        answer = existing or ScreeningKnowledgeAnswer(
            candidate_profile_id=candidate_profile.id,
            normalized_question=normalized,
        )
        answer.resume_id = resume.id
        answer.question_text = f"{skill} years experience"
        answer.category = "skill_years"
        answer.answer = str(event["selected_answer"])
        answer.source = event.get("source") or "Skill Experience Estimator"
        answer.confidence = event.get("confidence") or "Estimated"
        merged_evidence = dict(evidence)
        merged_evidence["normalized_skill"] = skill
        merged_evidence["saved_from_question"] = event.get("question_text")
        answer.evidence = merged_evidence
        db.add(answer)


def _save_form_learning_observations(
    db: Session,
    job: JobOpportunity,
    result: AutomationResult,
) -> None:
    try:
        learned = FormLearningEngine(settings).learn_from_application_result(db, job, result)
    except Exception:
        logger.exception("Form learning failed for job %s", job.id)
        return
    try:
        generalized = GeneralizedFormEngine(settings)
        fields: list[GeneralizedFieldInput] = []
        detections = []
        validation_results = []
        for record in learned:
            validation_examples = record.validation_examples or []
            validation_message = validation_examples[-1].get("message") if validation_examples else None
            field = GeneralizedFieldInput(
                label=record.original_label,
                input_type=record.field_type,
                validation_message=validation_message,
                portal="linkedin",
            )
            detection = generalized.detect_field(field, learned=record)
            fields.append(field)
            detections.append(detection)
            success = int(record.times_successful or 0) >= int(record.times_failed or 0)
            generalized.record_metadata(
                db,
                field,
                detection,
                success=success,
                validation_message=validation_message,
            )
            validation_results.append(
                {
                    "field": record.original_label,
                    "normalized_field": detection.normalized_field_name,
                    "success": success,
                    "validation_message": validation_message,
                }
            )
        if fields:
            generalized.write_debug(
                generalized.create_debug_dir(),
                fields,
                detections,
                validation_results=validation_results,
            )
    except Exception:
        logger.exception("Generalized form metadata capture failed for job %s", job.id)


def _reasoning_record_fields(result: AutomationResult) -> dict:
    events = [
        item
        for item in (result.screening_answers or [])
        if item.get("reasoning_used") or item.get("reasoning_type")
    ]
    if not events:
        for log_event in result.log or []:
            if log_event.get("event") == "screening_questions_answered":
                events.extend(
                    item
                    for item in log_event.get("answers", [])
                    if item.get("reasoning_used") or item.get("reasoning_type")
                )
    if not events:
        return {
            "reasoning_type": None,
            "reasoning_summary": None,
            "confidence_score": None,
            "evidence_sources": [],
        }
    primary = events[0]
    return {
        "reasoning_type": primary.get("reasoning_type"),
        "reasoning_summary": primary.get("reasoning_summary") or primary.get("reason"),
        "confidence_score": primary.get("confidence"),
        "evidence_sources": [
            {
                "question": event.get("question_text"),
                "answer": event.get("selected_answer"),
                "confidence": event.get("confidence"),
                "reasoning_type": event.get("reasoning_type"),
                "evidence": (event.get("evidence") or {}).get("evidence_used", []),
            }
            for event in events
        ],
    }


def _resume_upload_diagnostics_from_result(result: AutomationResult) -> dict:
    if result.resume_upload_diagnostics:
        return result.resume_upload_diagnostics
    for event in reversed(result.log or []):
        if event.get("event") != "resume_upload_check":
            continue
        detail = event.get("detail")
        if not detail:
            continue
        try:
            parsed = json.loads(detail)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _resume_upload_field(result: AutomationResult, key: str) -> str | None:
    value = _resume_upload_diagnostics_from_result(result).get(key)
    return str(value) if value is not None else None


def _resume_upload_status(result: AutomationResult) -> str | None:
    diagnostics = _resume_upload_diagnostics_from_result(result)
    if not diagnostics:
        return None
    if diagnostics.get("mismatch_detected"):
        return "mismatch"
    if diagnostics.get("upload_success"):
        return "verified"
    if diagnostics.get("upload_completed"):
        return "failed"
    if diagnostics.get("file_exists"):
        return "not_required_yet"
    return "missing_file"


@app.get("/applications", response_model=list[ApplicationHistoryOut])
def list_applications(
    resume_id: int | None = None,
    search_batch_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    statement = select(ApplicationRecord).join(JobOpportunity)
    if resume_id:
        statement = statement.where(JobOpportunity.resume_id == resume_id)
    if search_batch_id:
        statement = statement.where(JobOpportunity.search_batch_id == search_batch_id)
    records = list(db.scalars(statement.order_by(desc(ApplicationRecord.application_date))).all())
    return [
        {
            "id": record.id,
            "job_id": record.job_id,
            "status": record.status,
            "application_date": record.application_date,
            "attempt_count": record.attempt_count,
            "dry_run": record.dry_run,
            "current_step": record.current_step,
            "evidence_path": record.evidence_path,
            "resume_filename_used": record.resume_filename_used,
            "resume_upload_status": record.resume_upload_status,
            "resume_upload_evidence_path": record.resume_upload_evidence_path,
            "reasoning_type": record.reasoning_type,
            "reasoning_summary": record.reasoning_summary,
            "confidence_score": record.confidence_score,
            "evidence_sources": record.evidence_sources,
            "error_message": record.error_message,
            "run_log": record.run_log,
            "job_title": record.job.title,
            "company": record.job.company,
            "job_url": record.job.job_url,
        }
        for record in records
    ]


@app.get("/dashboard/summary")
def dashboard_summary(
    resume_id: int | None = None,
    search_batch_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    job_statement = select(JobOpportunity)
    if resume_id:
        job_statement = job_statement.where(JobOpportunity.resume_id == resume_id)
        if search_batch_id is None:
            latest_batch = latest_search_batch(db, resume_id)
            if latest_batch:
                search_batch_id = latest_batch.id
    if search_batch_id:
        job_statement = job_statement.where(JobOpportunity.search_batch_id == search_batch_id)
    jobs = list(db.scalars(job_statement).all())
    job_ids = [job.id for job in jobs]
    if not job_ids:
        applications = []
    else:
        applications = list(
            db.scalars(select(ApplicationRecord).where(ApplicationRecord.job_id.in_(job_ids))).all()
        )
    return {
        "matched_jobs": len(jobs),
        "easy_apply_jobs": sum(1 for job in jobs if job.easy_apply),
        "applied_jobs": sum(1 for item in applications if item.status == ApplicationStatus.APPLIED),
        "needs_review": sum(
            1 for item in applications if item.status == ApplicationStatus.NEEDS_REVIEW
        ),
        "failed_jobs": sum(
            1 for item in applications if item.status == ApplicationStatus.FAILED
        ),
        "skipped_jobs": sum(
            1 for item in applications if item.status == ApplicationStatus.SKIPPED
        ),
    }


@app.get("/performance/latest")
def performance_latest() -> dict:
    summary = latest_performance_summary(settings)
    if not summary:
        return {"available": False}
    summary["available"] = True
    return summary


@app.get("/reasoning/latest")
def reasoning_latest() -> dict:
    root = settings.reasoning_debug_dir
    if not root.exists():
        return {"available": False}
    candidates = [path for path in root.glob("**/question_reasoning.json") if path.is_file()]
    if not candidates:
        candidates = [path for path in root.glob("**/semantic_matches.json") if path.is_file()]
    if not candidates:
        return {"available": False}
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    folder = latest.parent

    def read_json(name: str):
        path = folder / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    return {
        "available": True,
        "folder": str(folder),
        "job_description_analysis": read_json("job_description_analysis.json"),
        "reasoning_steps": read_json("reasoning_steps.json"),
        "semantic_matches": read_json("semantic_matches.json"),
        "confidence_scores": read_json("confidence_scores.json"),
        "evidence_used": read_json("evidence_used.json"),
        "question_reasoning": read_json("question_reasoning.json"),
    }


@app.get("/resume-intelligence/latest")
def resume_intelligence_latest() -> dict:
    root = settings.resume_intelligence_debug_dir
    if not root.exists():
        return {"available": False}
    candidates = [path for path in root.glob("**/ranking_analysis.json") if path.is_file()]
    if not candidates:
        return {"available": False}
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    folder = latest.parent

    def read_json(name: str):
        path = folder / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    return {
        "available": True,
        "folder": str(folder),
        "ranking_analysis": read_json("ranking_analysis.json"),
        "match_explanation": read_json("match_explanation.json"),
        "generated_answers": read_json("generated_answers.json"),
        "cover_letter": read_json("cover_letter.json"),
        "resume_suggestions": read_json("resume_suggestions.json"),
        "reasoning_summary": read_json("reasoning_summary.json"),
    }


@app.get("/form-learning")
def list_form_learning(db: Session = Depends(get_db)) -> list[dict]:
    records = list(
        db.scalars(
            select(FormFieldLearning).order_by(
                desc(FormFieldLearning.last_seen),
                desc(FormFieldLearning.times_successful),
            )
        ).all()
    )
    grouped: dict[str, dict] = {}
    for record in records:
        key = record.normalized_field
        item = grouped.setdefault(
            key,
            {
                "normalized_field": record.normalized_field,
                "field_category": record.field_category,
                "original_labels": [],
                "companies_seen": [],
                "roles_seen": [],
                "times_seen": 0,
                "times_successful": 0,
                "times_failed": 0,
                "times_used": 0,
                "confidence": record.confidence,
                "validation_history": [],
                "last_successful_value_source": None,
                "last_seen": record.last_seen,
            },
        )
        if record.original_label not in item["original_labels"]:
            item["original_labels"].append(record.original_label)
        if record.company and record.company not in item["companies_seen"]:
            item["companies_seen"].append(record.company)
        if record.role and record.role not in item["roles_seen"]:
            item["roles_seen"].append(record.role)
        item["times_seen"] += int(record.times_seen or 0)
        item["times_successful"] += int(record.times_successful or 0)
        item["times_failed"] += int(record.times_failed or 0)
        item["times_used"] += int(record.times_used or 0)
        item["validation_history"].extend(record.validation_examples or [])
        if record.last_successful_value_source:
            item["last_successful_value_source"] = record.last_successful_value_source
        if record.last_seen and record.last_seen > item["last_seen"]:
            item["last_seen"] = record.last_seen
            item["confidence"] = record.confidence
    for item in grouped.values():
        seen = max(item["times_seen"], 1)
        item["success_rate"] = round((item["times_successful"] / seen) * 100, 2)
        item["validation_history"] = item["validation_history"][-10:]
    return sorted(grouped.values(), key=lambda item: (item["times_seen"], item["success_rate"]), reverse=True)


@app.get("/form-learning/latest")
def form_learning_latest() -> dict:
    root = settings.form_learning_debug_dir
    if not root.exists():
        return {"available": False}
    candidates = [path for path in root.glob("**/learned_fields.json") if path.is_file()]
    if not candidates:
        return {"available": False}
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    folder = latest.parent

    def read_json(name: str):
        path = folder / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    return {
        "available": True,
        "folder": str(folder),
        "learned_fields": read_json("learned_fields.json"),
        "field_normalization": read_json("field_normalization.json"),
        "validation_learning": read_json("validation_learning.json"),
        "confidence_updates": read_json("confidence_updates.json"),
        "field_statistics": read_json("field_statistics.json"),
        "reasoning_usage": read_json("reasoning_usage.json"),
    }


@app.get("/generalized-form-engine")
def list_generalized_form_metadata(db: Session = Depends(get_db)) -> list[dict]:
    records = list(
        db.scalars(
            select(GeneralizedFormFieldMetadata).order_by(
                desc(GeneralizedFormFieldMetadata.last_used_at),
                desc(GeneralizedFormFieldMetadata.times_successful),
            )
        ).all()
    )
    return [
        {
            "portal": record.portal,
            "original_field_label": record.original_field_label,
            "normalized_field_name": record.normalized_field_name,
            "field_category": record.field_category,
            "detected_control_type": record.detected_control_type,
            "recommended_fill_strategy": record.recommended_fill_strategy,
            "confidence": record.confidence,
            "times_seen": record.times_seen,
            "times_successful": record.times_successful,
            "times_failed": record.times_failed,
            "last_validation_message": record.last_validation_message,
            "last_used_at": record.last_used_at,
        }
        for record in records
    ]


@app.get("/generalized-form-engine/latest")
def generalized_form_engine_latest() -> dict:
    root = settings.generalized_form_debug_dir
    if not root.exists():
        return {"available": False}
    candidates = [path for path in root.glob("**/detected_fields.json") if path.is_file()]
    if not candidates:
        return {"available": False}
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    folder = latest.parent

    def read_json(name: str):
        path = folder / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    return {
        "available": True,
        "folder": str(folder),
        "detected_fields": read_json("detected_fields.json"),
        "control_types": read_json("control_types.json"),
        "fill_strategies": read_json("fill_strategies.json"),
        "learning_reuse": read_json("learning_reuse.json"),
        "validation_results": read_json("validation_results.json"),
        "pipeline_integration": read_json("pipeline_integration.json"),
    }
