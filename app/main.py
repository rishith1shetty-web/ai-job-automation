import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from playwright.sync_api import Error as PlaywrightError
from sqlalchemy import Integer, asc, cast, desc, func, select
from sqlalchemy.orm import Session, contains_eager

from app.config import get_settings
# The MODULE, not the name: "from app.database import SessionLocal" resolves the lazy
# attribute at IMPORT time, which builds the engine and so demands a database credential
# before the launcher can report that none is stored yet. Through the module it stays lazy.
from app import database
from app.database import get_db, init_db
from app.logging_config import configure_logging
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    ApplyTopBatch,
    CandidateProfile,
    CandidateEmploymentHistory,
    FormFieldLearning,
    GeneralizedFormFieldMetadata,
    JobOpportunity,
    JobSearchBatch,
    PortalRuntimeState,
    ResumeProfile,
    ScreeningAutomationSetting,
    ScreeningKnowledgeAnswer,
)
from app.schemas import (
    ApplicationHistoryOut,
    ApplicationRecordOut,
    ApplyTopBatchOut,
    AutomationStateOut,
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
from app.services.role_selection import MAX_SEARCH_ROLES
from app.services.application_outcome import (
    DRY_RUN_COMPLETE_STEPS,
    SUCCESS_APPLICATION_STATUSES,
    outcome_supersedes,
    reset_attempt_specific_fields,
)
from app.services.resume_identity import (
    compute_resume_fingerprint,
    resume_family_ids_for,
    successful_jobs_for_fingerprint,
)
from app.services.automation_control import (
    AUTOMATION_IDLE,
    BATCH_STATUS_INTERRUPTED,
    BATCH_STATUS_STOPPED,
    BATCH_STATUS_STOPPING,
    JOB_RESULT_STOPPED,
    RESUMABLE_BATCH_STATUSES,
    AutomationCancelled,
    BatchAlreadyActive,
    get_automation_controller,
    reset_automation_controller,
)
from app.services.runtime_session import get_runtime_session, reset_runtime_session
from app.services.form_learning import FormLearningEngine
from app.services.generalized_form_engine import GeneralizedFieldInput, GeneralizedFormEngine
from app.services.matcher import MatchScorer
from app.services.performance import PerformanceRecorder, latest_performance_summary
from app.services.portals.dice_portal import DicePortalAdapter
from app.services.reasoning import AIReasoningEngine
from app.services.resume_intelligence import ResumeIntelligenceEngine
from app.services.resume_parser import ResumeParser, apply_role_profile_compat
from app.static_site import mount_frontend

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()
app = FastAPI(title="LinkedIn AI Job Matcher", version="0.1.0")
SEARCH_PLAN_CACHE: dict[tuple, tuple[list, dict]] = {}


def normalize_portal(value: str | None) -> str:
    portal = (value or "linkedin").strip().lower()
    if portal == "both":
        raise HTTPException(status_code=400, detail="Single active portal mode supports LinkedIn or Dice only.")
    if portal not in {"linkedin", "dice"}:
        raise HTTPException(status_code=400, detail="Portal must be LinkedIn or Dice.")
    return portal


def search_portals(value: str | None) -> list[str]:
    return [normalize_portal(value)]


def dice_adapter() -> DicePortalAdapter:
    return DicePortalAdapter(settings)


def active_portal_state(db: Session) -> PortalRuntimeState:
    state = db.get(PortalRuntimeState, 1)
    if state is None:
        state = PortalRuntimeState(id=1, active_portal="linkedin")
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def get_active_portal(db: Session) -> str:
    return normalize_portal(active_portal_state(db).active_portal)


def set_active_portal(db: Session, portal: str) -> PortalRuntimeState:
    requested = normalize_portal(portal)
    state = active_portal_state(db)
    state.active_portal = requested
    state.updated_at = datetime.utcnow()
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def disconnect_inactive_portal(active: str) -> None:
    active = normalize_portal(active)
    if active == "linkedin":
        dice_adapter().disconnect()
    elif active == "dice":
        LinkedInAutomation(settings).disconnect_account()


def request_portal_or_active(db: Session, portal: str | None = None) -> str:
    return normalize_portal(portal or get_active_portal(db))


def require_active_portal(db: Session, portal: str | None = None) -> str:
    requested = request_portal_or_active(db, portal)
    active = get_active_portal(db)
    if requested != active:
        raise HTTPException(status_code=409, detail=f"Active portal is {active.title()}. Switch portals before using {requested.title()}.")
    return active


def resolve_search_query(resume: ResumeProfile, manual_keywords: str | None) -> str:
    manual_override = (manual_keywords or "").strip()
    if manual_override:
        return manual_override
    if resume.generated_search_query:
        return resume.generated_search_query
    search_terms = resume.generated_search_keywords[:5] or [resume.detected_role or "Software Engineer"]
    return " OR ".join(search_terms)


def latest_search_batch(db: Session, resume_id: int, portal: str | None = None) -> JobSearchBatch | None:
    statement = select(JobSearchBatch).where(JobSearchBatch.resume_id == resume_id)
    if portal:
        statement = statement.where(JobSearchBatch.portal == normalize_portal(portal))
    return db.scalar(statement.order_by(desc(JobSearchBatch.created_at)).limit(1))


def default_candidate_profile(db: Session, portal: str | None = None) -> CandidateProfile | None:
    requested = request_portal_or_active(db, portal)
    return db.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.is_default.is_(True))
        .where(CandidateProfile.portal == requested)
        .order_by(desc(CandidateProfile.updated_at), desc(CandidateProfile.created_at))
        .limit(1)
    )


def get_or_create_candidate_profile(db: Session, portal: str | None = None) -> CandidateProfile:
    requested = request_portal_or_active(db, portal)
    profile = default_candidate_profile(db, requested)
    if profile is None:
        profile = CandidateProfile(is_default=True, portal=requested)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def normalized_screening_question(question: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    replacements = {
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
    # Every backend start begins with a clean active dashboard session: no portal marked
    # connected, no active resume, no batch, no matches, zeroed counters. Persistent data
    # (resumes, batches, jobs, history) and saved portal storage-state files are untouched;
    # the user reaches them again by explicitly connecting a portal and re-analysing a
    # resume (which reattaches to its existing fingerprint family).
    reset_runtime_session()
    # A fresh process cannot have inherited a running apply batch. Without this a claim left
    # behind by a crash would block every future Apply Jobs run.
    reset_automation_controller()
    # ...and any batch still recorded as running belonged to a process that is gone. Adopt it
    # as interrupted so Continue Automation can offer it instead of it looking active forever.
    _adopt_interrupted_batches()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/portal/active")
def get_active_portal_status() -> dict:
    # The ACTIVE session, not persisted preference and not "a storage-state file exists".
    # A portal is connected only after an explicit Connect in this process, so a fresh
    # backend start always reports both portals disconnected with no active portal.
    # Read-only on purpose: this endpoint must never delete a saved portal session.
    session = get_runtime_session()
    return {
        "active_portal": session.active_portal,
        "linkedin_connected": session.linkedin_connected,
        "dice_connected": session.dice_connected,
    }


def _runtime_session_payload(session) -> dict:
    """Serialise the in-memory active session for the dashboard. Pure read of runtime state --
    no persistent data (resumes, batches, jobs, history) is consulted or reported here."""
    return {
        "active_portal": session.active_portal,
        "linkedin_connected": session.linkedin_connected,
        "dice_connected": session.dice_connected,
        "active_resume_id": session.active_resume_id,
        "active_search_batch_id": session.active_search_batch_id,
        "current_matches": session.current_matches,
        "current_apply_queue": session.current_apply_queue,
        "current_progress": session.current_progress,
        "current_run_status": session.current_run_status,
        "current_run_error": session.current_run_error,
    }


@app.post("/session/reset")
def reset_session() -> dict:
    """Start a fresh dashboard session WITHOUT restarting the backend -- the same clean state a
    process gets on startup. Resets only the in-memory RuntimeSession and returns it.

    Deliberately scoped: it performs no database read/write, unlinks no file, and never calls
    LinkedIn/Dice automation. In particular it does NOT call the explicit portal disconnect
    functions (those delete saved storage-state files); a portal simply reads as disconnected
    until the user explicitly Connects again, which can still reuse the saved session. Resetting
    an already-clean session is a no-op, so this is safe to call repeatedly.
    """
    session = reset_runtime_session()
    return _runtime_session_payload(session)


@app.post("/portal/active/{portal}")
def switch_active_portal(portal: str, db: Session = Depends(get_db)) -> dict:
    # normalize_portal rejects anything that is not LinkedIn/Dice with a 400.
    requested = normalize_portal(portal)
    # Persist the internal portal preference (endpoints that need a concrete portal still
    # resolve it from here)...
    set_active_portal(db, requested)
    # ...and mirror it into the runtime session. GET /portal/active reports the SESSION, so
    # without this the switch is invisible to the dashboard: it would keep reading
    # active_portal: null, fall back to the LinkedIn panel, and the other portal's Connect
    # button could never be reached.
    session = get_runtime_session()
    session.select_portal(requested)
    # Selecting a portal is NOT connecting: connected flags stay false until an explicit
    # Connect, and neither portal's saved storage-state file is deleted, so that Connect can
    # still reuse a valid saved session. No resume is required to switch.
    return {"active_portal": session.active_portal, "status": "active"}


@app.post("/resumes", response_model=ResumeProfileOut)
def upload_resume(upload: UploadFile = File(...), db: Session = Depends(get_db)) -> ResumeProfile:
    active = get_active_portal(db)
    parser = ResumeParser(settings)
    stored_path = parser.save_upload(upload)
    raw_text = parser.extract_text(stored_path)
    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from resume.")
    profile = parser.parse(raw_text)
    # Phase 4: the persisted/visible role analysis is the Phase-3 role_profile (not the legacy
    # ROLE_PROFILES output). Search variations read role_profile directly; these mapped fields drive
    # the dashboard display and the legacy-compatible fallback.
    apply_role_profile_compat(profile)
    resume = ResumeProfile(
        portal=active,
        filename=upload.filename or Path(stored_path).name,
        stored_path=str(stored_path.resolve()),
        raw_text=raw_text,
        resume_fingerprint=compute_resume_fingerprint(raw_text),
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
        domain_profile=profile.get("domain_profile") or {},
        role_profile=profile.get("role_profile") or {},
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    # Analysing a resume is the explicit action that makes it active for this session. The
    # fingerprint above reattaches it to its existing family, so the candidate profile,
    # employment history, screening answers and application history for that resume identity
    # come back with it -- while any PREVIOUS session's batch/matches stay cleared, so old
    # Find Matches results never reappear on re-analysis.
    get_runtime_session().activate_resume(resume.id)

    candidate = get_or_create_candidate_profile(db, active)
    db.add(candidate)
    resume_profile = parser.extract_candidate_profile(raw_text, parser.extract_hyperlinks(stored_path))
    merge_candidate_profile(candidate, resume_profile)
    linkedin_profile = None
    if active == "linkedin" and LinkedInAutomation(settings).has_authenticated_session():
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
    # The resume ACTIVE IN THIS SESSION -- never "the newest row in the database". A fresh
    # backend start has none, so the dashboard shows no analysed resume until the user
    # uploads and analyses one again (which reattaches to its existing fingerprint family
    # and brings its persistent profile/history back with it).
    session = get_runtime_session()
    if session.active_resume_id is None:
        raise HTTPException(status_code=404, detail="No resume has been analyzed.")
    resume = db.get(ResumeProfile, session.active_resume_id)
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
    try:
        automation.connect_account()
    except (PlaywrightError, OSError) as exc:
        # The browser/session could not be launched (e.g. Playwright driver subprocess
        # failed to start). Surface a clear, actionable error instead of a raw 500.
        logger.exception("LinkedIn connect could not launch the browser session.")
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not launch the LinkedIn browser session. Start the API from the "
                "project virtual environment via start_app.ps1 and ensure Playwright "
                f"browsers are installed. ({type(exc).__name__}: {exc})"
            ),
        ) from exc
    dice_adapter().disconnect()
    with database.SessionLocal() as db:
        set_active_portal(db, "linkedin")
    # Explicit user Connect -> this session may now show LinkedIn as connected.
    get_runtime_session().mark_connected("linkedin")
    return {"status": "connected", "storage_state": str(settings.linkedin_storage_state)}


@app.get("/linkedin/status")
def linkedin_status() -> dict:
    # Session-scoped: a saved storage-state file on disk must not read as connected.
    return {"connected": get_runtime_session().linkedin_connected}


@app.post("/linkedin/disconnect")
def disconnect_linkedin() -> dict:
    LinkedInAutomation(settings).disconnect_account()
    get_runtime_session().mark_disconnected("linkedin")
    return {"status": "disconnected"}


@app.post("/dice/connect")
def connect_dice() -> dict:
    result = dice_adapter().connect()
    if result.get("status") != "connected":
        raise HTTPException(status_code=409, detail=result.get("reason") or "Dice login was not verified.")
    LinkedInAutomation(settings).disconnect_account()
    with database.SessionLocal() as db:
        set_active_portal(db, "dice")
    # Explicit user Connect -> this session may now show Dice as connected.
    get_runtime_session().mark_connected("dice")
    return result


@app.get("/dice/status")
def dice_status() -> dict:
    # Session-scoped: a saved storage-state file on disk must not read as connected.
    return {"connected": get_runtime_session().dice_connected}


@app.post("/dice/disconnect")
def disconnect_dice() -> dict:
    dice_adapter().disconnect()
    get_runtime_session().mark_disconnected("dice")
    return {"status": "disconnected"}


@app.get("/candidate-profile", response_model=CandidateProfileOut)
def get_candidate_profile(db: Session = Depends(get_db)) -> CandidateProfile:
    return get_or_create_candidate_profile(db)


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
    candidate = get_or_create_candidate_profile(db)
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
    profile = get_or_create_candidate_profile(db)
    for field, value in payload.model_dump().items():
        if field == "portal":
            continue
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
    candidate = get_or_create_candidate_profile(db)
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


def _search_batch_out(db: Session, batch: JobSearchBatch) -> dict:
    job_count = (
        db.scalar(select(func.count(JobOpportunity.id)).where(JobOpportunity.search_batch_id == batch.id))
        or 0
    )
    easy_apply_count = (
        db.scalar(
            select(func.count(JobOpportunity.id)).where(
                JobOpportunity.search_batch_id == batch.id,
                JobOpportunity.easy_apply.is_(True),
            )
        )
        or 0
    )
    return {
        "id": batch.id,
        "resume_id": batch.resume_id,
        "search_query": batch.search_query,
        "location": batch.location,
        "portal": batch.portal,
        "status": batch.status,
        "requested_limit": batch.requested_limit,
        "current_search_variation": batch.current_search_variation,
        "jobs_collected": max(batch.jobs_collected or 0, job_count),
        "easy_apply_count": max(batch.easy_apply_count or 0, easy_apply_count),
        "duplicates_removed": batch.duplicates_removed,
        "latest_status_message": batch.latest_status_message,
        "error_message": batch.error_message,
        "debug_path": batch.debug_path,
        "started_at": batch.started_at,
        "finished_at": batch.finished_at,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "job_count": job_count,
    }


def _create_find_matches_debug_dir(batch_id: str) -> Path:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    debug_dir = settings.find_matches_batch_debug_dir / timestamp / batch_id
    debug_dir.mkdir(parents=True, exist_ok=True)
    return debug_dir


def _write_find_matches_batch_debug(debug_dir: Path | None, filename: str, payload: dict) -> None:
    if not debug_dir:
        return
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _search_conflict_context(
    db: Session,
    request: SearchRequest,
    resume: ResumeProfile | None,
    *,
    condition: str,
    message: str,
) -> dict:
    active = get_active_portal(db)
    request_portal_raw = request.portal
    try:
        resolved_request_portal = normalize_portal(request.portal or active)
    except HTTPException:
        resolved_request_portal = str(request.portal)
    linkedin_connected = LinkedInAutomation(settings).has_authenticated_session()
    dice_connected = dice_adapter().is_connected()
    authenticated_portals = []
    if linkedin_connected:
        authenticated_portals.append("linkedin")
    if dice_connected:
        authenticated_portals.append("dice")
    latest_batch = None
    if resume is not None:
        batch_portal = resolved_request_portal if resolved_request_portal in {"linkedin", "dice"} else None
        batch = latest_search_batch(db, resume.id, batch_portal)
        if batch:
            latest_batch = {
                "id": batch.id,
                "portal": batch.portal,
                "status": batch.status,
                "requested_limit": batch.requested_limit,
                "jobs_collected": batch.jobs_collected,
                "latest_status_message": batch.latest_status_message,
                "error_message": batch.error_message,
                "created_at": batch.created_at,
                "updated_at": batch.updated_at,
            }
    return {
        "condition": condition,
        "message": message,
        "active_portal": active,
        "request_portal": request_portal_raw,
        "resolved_request_portal": resolved_request_portal,
        "resume_id": request.resume_id,
        "resume_portal": resume.portal if resume else None,
        "session_status": {
            "linkedin_connected": linkedin_connected,
            "dice_connected": dice_connected,
        },
        "authenticated_portal": authenticated_portals,
        "search_batch_state": latest_batch,
        "requested_location": request.location,
        "requested_limit": request.limit,
    }


def _raise_search_conflict(
    db: Session,
    request: SearchRequest,
    resume: ResumeProfile | None,
    *,
    condition: str,
    message: str,
) -> None:
    context = _search_conflict_context(db, request, resume, condition=condition, message=message)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    debug_dir = settings.find_matches_batch_debug_dir / timestamp / "search_conflict"
    _write_find_matches_batch_debug(debug_dir, "search_conflict.json", context)
    logger.warning("Find Matches conflict: %s", json.dumps(context, default=str))
    detail = (
        f"{message} "
        f"(condition={condition}; active_portal={context['active_portal']}; "
        f"request_portal={context['resolved_request_portal']}; "
        f"resume_portal={context['resume_portal']}; "
        f"linkedin_connected={context['session_status']['linkedin_connected']}; "
        f"dice_connected={context['session_status']['dice_connected']}; "
        f"debug_path={debug_dir})"
    )
    raise HTTPException(status_code=409, detail=detail)


def _create_search_batch(request: SearchRequest, db: Session) -> JobSearchBatch:
    resume = db.get(ResumeProfile, request.resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")
    active = get_active_portal(db)
    portal = request_portal_or_active(db, request.portal)
    if portal != active:
        _raise_search_conflict(
            db,
            request,
            resume,
            condition="requested_portal_not_active",
            message=f"Find Matches is set to {portal.title()}, but the active portal is {active.title()}. Switch active portal before searching.",
        )
    if resume.portal != portal:
        _raise_search_conflict(
            db,
            request,
            resume,
            condition="resume_portal_mismatch",
            message=(
                f"The active resume belongs to {resume.portal.title()}, but Find Matches is running in {portal.title()} mode. "
                f"Analyze/upload the resume again while {portal.title()} is the active portal."
            ),
        )
    portals = search_portals(portal)
    if "linkedin" in portals and not settings.linkedin_storage_state.exists():
        _raise_search_conflict(
            db,
            request,
            resume,
            condition="linkedin_session_missing",
            message="LinkedIn is the active search portal, but no LinkedIn session file was found. Connect LinkedIn before searching.",
        )
    if "dice" in portals and not dice_adapter().is_connected():
        _raise_search_conflict(
            db,
            request,
            resume,
            condition="dice_session_missing_or_unverified",
            message="Dice is the active search portal, but the saved Dice session is missing or not verified. Connect Dice before searching Dice jobs.",
        )
    batch = JobSearchBatch(
        id=str(uuid4()),
        resume_id=resume.id,
        search_query=resolve_search_query(resume, request.keywords),
        manual_keywords=request.keywords,
        location=request.location,
        portal=portal,
        status="queued",
        requested_limit=request.limit,
        jobs_collected=0,
        easy_apply_count=0,
        duplicates_removed=0,
        latest_status_message="Search queued.",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _update_search_batch(
    db: Session,
    batch: JobSearchBatch,
    *,
    status: str | None = None,
    current_search_variation: str | None = None,
    current_variation_index: int | None = None,
    total_variations: int | None = None,
    jobs_found_this_variation: int | None = None,
    jobs_collected: int | None = None,
    easy_apply_count: int | None = None,
    duplicates_removed: int | None = None,
    latest_status_message: str | None = None,
    error_message: str | None = None,
    debug_path: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    if status is not None:
        batch.status = status
    if current_search_variation is not None:
        batch.current_search_variation = current_search_variation
    if current_variation_index is not None:
        batch.current_variation_index = current_variation_index
    if total_variations is not None:
        batch.total_variations = total_variations
    if jobs_found_this_variation is not None:
        batch.jobs_found_this_variation = jobs_found_this_variation
    if jobs_collected is not None:
        batch.jobs_collected = jobs_collected
    if easy_apply_count is not None:
        batch.easy_apply_count = easy_apply_count
    if duplicates_removed is not None:
        batch.duplicates_removed = duplicates_removed
    if latest_status_message is not None:
        batch.latest_status_message = latest_status_message
    if error_message is not None:
        batch.error_message = error_message
    if debug_path is not None:
        batch.debug_path = debug_path
    if started_at is not None:
        batch.started_at = started_at
    if finished_at is not None:
        batch.finished_at = finished_at
    batch.updated_at = datetime.utcnow()
    db.add(batch)
    db.commit()


@app.post("/jobs/search", response_model=SearchBatchOut)
def search_jobs(
    request: SearchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    batch = _create_search_batch(request, db)
    background_tasks.add_task(_run_find_matches_batch, batch.id)
    return _search_batch_out(db, batch)


def _run_find_matches_batch(batch_id: str) -> None:
    debug_dir = _create_find_matches_debug_dir(batch_id)
    with database.SessionLocal() as db:
        batch = db.get(JobSearchBatch, batch_id)
        if not batch:
            return
        _update_search_batch(
            db,
            batch,
            status="running",
            latest_status_message=f"Starting {batch.portal.title()} job search.",
            debug_path=str(debug_dir),
            started_at=datetime.utcnow(),
        )
        _write_find_matches_batch_debug(
            debug_dir,
            "batch_started.json",
            {
                "batch_id": batch.id,
                "resume_id": batch.resume_id,
                "location": batch.location,
                "portal": batch.portal,
                "requested_limit": batch.requested_limit,
                "started_at": batch.started_at,
            },
        )
        try:
            request = SearchRequest(
                resume_id=batch.resume_id,
                location=batch.location,
                keywords=batch.manual_keywords,
                limit=batch.requested_limit,
                portal=batch.portal,
            )
            ranked = _search_jobs_sync(request, db, batch=batch, batch_debug_dir=debug_dir)
            _update_search_batch(
                db,
                batch,
                status="completed",
                current_search_variation="",
                jobs_collected=len(ranked),
                easy_apply_count=sum(1 for job in ranked if job.easy_apply),
                latest_status_message=(
                    f"Found {len(ranked)} jobs out of requested {batch.requested_limit} based on current {batch.portal.title()} results."
                    if len(ranked) < int(batch.requested_limit or 0)
                    else f"Search complete. Saved {len(ranked)} ranked jobs."
                ),
                finished_at=datetime.utcnow(),
            )
            _write_find_matches_batch_debug(
                debug_dir,
                "batch_completed.json",
                {
                    "batch_id": batch.id,
                    "status": "completed",
                    "jobs_collected": len(ranked),
                    "easy_apply_count": sum(1 for job in ranked if job.easy_apply),
                    "finished_at": batch.finished_at,
                },
            )
        except Exception as exc:
            logger.exception("Find Matches batch %s failed", batch.id)
            _update_search_batch(
                db,
                batch,
                status="failed",
                error_message=str(exc),
                latest_status_message="Search failed in backend.",
                finished_at=datetime.utcnow(),
            )
            _write_find_matches_batch_debug(
                debug_dir,
                "batch_failed.json",
                {"batch_id": batch.id, "status": "failed", "error": str(exc)},
            )


def _search_jobs_sync(
    request: SearchRequest,
    db: Session,
    *,
    batch: JobSearchBatch | None = None,
    batch_debug_dir: Path | None = None,
) -> list[JobOpportunity]:
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
        active = get_active_portal(db)
        portal = request_portal_or_active(db, request.portal)
        if portal != active:
            _raise_search_conflict(
                db,
                request,
                resume,
                condition="requested_portal_not_active",
                message=f"Find Matches is set to {portal.title()}, but the active portal is {active.title()}. Switch active portal before searching.",
            )
        if resume.portal != portal:
            _raise_search_conflict(
                db,
                request,
                resume,
                condition="resume_portal_mismatch",
                message=(
                    f"Resume belongs to {resume.portal.title()}, but the active search portal is {portal.title()}. "
                    f"Analyze/upload the resume again while {portal.title()} is the active portal."
                ),
            )
        portals = search_portals(portal)
        with recorder.stage("portal_session_validation", portals=",".join(portals)):
            if "linkedin" in portals and not settings.linkedin_storage_state.exists():
                _raise_search_conflict(
                    db,
                    request,
                    resume,
                    condition="linkedin_session_missing",
                    message="LinkedIn is the active search portal, but no LinkedIn session file was found. Connect LinkedIn before searching.",
                )
            if "dice" in portals and not dice_adapter().is_connected():
                _raise_search_conflict(
                    db,
                    request,
                    resume,
                    condition="dice_session_missing_or_unverified",
                    message="Dice is the active search portal, but the saved Dice session is missing or not verified. Connect Dice before searching Dice jobs.",
                )

        engine = FindMatchesEngine(settings)
        debug_dir = engine.create_debug_dir()
        if batch is not None:
            _update_search_batch(
                db,
                batch,
                latest_status_message="Building search variations.",
                debug_path=str(batch_debug_dir) if batch_debug_dir else batch.debug_path,
            )
        cache_key = (
            portal,
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
        if batch is None:
            search_timestamp = datetime.utcnow()
            search_batch = JobSearchBatch(
                id=str(uuid4()),
                resume_id=resume.id,
                search_query=search_query,
                manual_keywords=request.keywords,
                location=request.location,
                portal=portal,
                status="running",
                requested_limit=request.limit,
                created_at=search_timestamp,
                updated_at=search_timestamp,
            )
            db.add(search_batch)
            db.commit()
        else:
            search_batch = batch
            search_batch.search_query = search_query
            search_batch.manual_keywords = request.keywords
            search_batch.location = request.location
            search_batch.portal = normalize_portal(request.portal)
            search_batch.requested_limit = request.limit
            search_timestamp = search_batch.created_at or datetime.utcnow()
            db.add(search_batch)
            db.commit()

        engine.write_debug(debug_dir, "search_queries.json", search_plan["queries"])
        engine.write_debug(debug_dir, "expanded_roles.json", search_plan["expanded_roles"])
        engine.write_debug(debug_dir, "expanded_skills.json", search_plan["expanded_skills"])
        engine.write_debug(debug_dir, "location_expansion.json", search_plan["locations"])

        target_count = engine.target_count(request.limit)
        jobs: list[JobOpportunity] = []
        seen_keys: set[str] = set()
        duplicate_jobs: list[dict] = []
        ineligible_jobs: list[dict] = []
        jobs_found: list[dict] = []
        jobs_per_variation: list[dict] = []
        with recorder.stage("duplicate_key_loading"):
            # NOTE: merely appearing in an EARLIER Find Matches batch must never exclude a
            # job. Only a confirmed live APPLIED outcome may (see below). Jobs whose history
            # is MATCHED / DRY_RUN_READY / FAILED / NEEDS_REVIEW / SKIPPED / TIMED_OUT /
            # UNSUPPORTED_FORM stay searchable and are collected again on every run.
            #
            # Portal-scoped exclusion of jobs already successfully APPLIED for this resume's
            # fingerprint family. A confirmed live submission must not reappear for the same
            # resume identity + portal — even after the resume is re-uploaded under a new id.
            # Keyed by portal so a LinkedIn success never blocks a Dice job (and vice versa).
            # Reuses the exact same stable job identity (engine.duplicate_key) as duplicate
            # detection. APPLIED only: a submit-ready dry run is not a submission.
            excluded_success_keys_by_portal: dict[str, set[str]] = {}
            for exclusion_portal in portals:
                success_keys: set[str] = set()
                for successful_job in successful_jobs_for_fingerprint(
                    db, resume, exclusion_portal, statuses=(ApplicationStatus.APPLIED,)
                ):
                    for key in engine.duplicate_key(
                        {
                            "job_url": successful_job.job_url,
                            "title": successful_job.title,
                            "company": successful_job.company,
                            "location": successful_job.location,
                        }
                    ):
                        if key:
                            success_keys.add(key)
                excluded_success_keys_by_portal[exclusion_portal] = success_keys
        collection_plan = engine.collection_plan(target_count, len(search_variations))
        selected_variations = search_variations[: collection_plan["selected_variation_count"]]
        limit_per_query = collection_plan["limit_per_query"]
        # Weighted CANDIDATE effort: the top-five variations inspect a larger per-query budget
        # than later ones, so they genuinely try to find more jobs (not just keep more). Every
        # variation is still searched. This is the search-side companion to the weighted
        # acceptance allocation below.
        search_limits = [
            limit_per_query * engine.TOP_VARIATION_WEIGHT if i < engine.TOP_VARIATION_COUNT else limit_per_query
            for i in range(len(selected_variations))
        ]
        if batch is not None:
            _update_search_batch(
                db,
                batch,
                status="running",
                current_search_variation=selected_variations[0].query if selected_variations else "",
                latest_status_message=(
                    f"Collecting jobs from {len(selected_variations)} search variations."
                    if selected_variations
                    else "No search variations were generated."
                ),
            )
        engine.write_debug(
            debug_dir,
            "search_collection_plan.json",
            {
                "requested_limit": request.limit,
                "target_count": target_count,
                "max_search_roles": MAX_SEARCH_ROLES,
                "available_variations": len(search_variations),
                "selected_variations": len(selected_variations),
                "selected_roles": [variation.role for variation in selected_variations],
                "limit_per_query": limit_per_query,
            },
        )
        variation_results: list[tuple[str, object, list[dict]]] = []
        # ONE progress mechanism for every portal. The callback contract is the one Dice already
        # used -- (variation, raw_jobs, index, total) with a 1-based index -- so no second,
        # incompatible progress system exists. `phase` is optional: Dice calls without it (its
        # published fields and message stay byte-identical), LinkedIn also reports "started" so
        # the dashboard shows the current variation while it is still searching. `raw_jobs` is
        # each portal's already-filtered per-variation result, so "jobs collected" means the same
        # thing for both portals and raw cards are never counted as jobs.
        def make_progress_callback(portal_label: str):
            totals = {"jobs": 0, "easy_apply": 0}

            def progress_callback(variation, raw_jobs, index, total, phase: str = "completed"):
                # Reporting must NEVER stop or fail the actual search.
                try:
                    query = getattr(variation, "query", "") or ""
                    if phase == "started":
                        message = f"{portal_label} search {index}/{total}: {query} searching."
                        found_now = 0
                    else:
                        totals["jobs"] += len(raw_jobs)
                        totals["easy_apply"] += sum(1 for item in raw_jobs if item.get("easy_apply"))
                        found_now = len(raw_jobs)
                        message = (
                            f"{portal_label} search {index}/{total}: {query} "
                            f"returned {found_now} jobs."
                        )
                    if batch is not None:
                        _update_search_batch(
                            db,
                            batch,
                            current_search_variation=query,
                            jobs_collected=totals["jobs"],
                            easy_apply_count=totals["easy_apply"],
                            current_variation_index=index,
                            total_variations=total,
                            jobs_found_this_variation=found_now,
                            latest_status_message=message,
                        )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("%s progress reporting failed: %s", portal_label, exc)

            return progress_callback

        for portal in portals:
            adapter = LinkedInAutomation(settings) if portal == "linkedin" else dice_adapter()
            with recorder.stage(
                f"{portal}_find_matches_page_loading",
                query_count=len(selected_variations),
                limit_per_query=limit_per_query,
            ):
                if portal == "linkedin":
                    if hasattr(adapter, "search_jobs_many"):
                        try:
                            portal_results = adapter.search_jobs_many(
                                resume,
                                selected_variations,
                                search_limits,
                                collect_card_diagnostics=False,
                                progress_callback=make_progress_callback("LinkedIn"),
                            )
                        except TypeError:
                            # An adapter that predates progress reporting still works unchanged.
                            portal_results = adapter.search_jobs_many(
                                resume,
                                selected_variations,
                                search_limits,
                                collect_card_diagnostics=False,
                            )
                    else:
                        portal_results = []
                        for i, variation in enumerate(selected_variations):
                            per_variation_limit = search_limits[i]
                            try:
                                raw_jobs = adapter.search_jobs(
                                    resume,
                                    variation.location,
                                    variation.query,
                                    per_variation_limit,
                                    collect_card_diagnostics=False,
                                )
                            except TypeError:
                                raw_jobs = adapter.search_jobs(resume, variation.location, variation.query, per_variation_limit)
                            portal_results.append((variation, raw_jobs))
                else:
                    portal_results = adapter.search_jobs_many(
                        resume,
                        selected_variations,
                        search_limits,
                        progress_callback=make_progress_callback("Dice"),
                        global_target=target_count,
                    )
            for variation, raw_jobs in portal_results:
                variation_results.append((portal, variation, raw_jobs))
        # Pass 1 -- canonical dedup in variation-PRIORITY order. The first (highest-priority)
        # variation that finds a job OWNS it; a later/lower variation can neither move nor
        # duplicate it. This is where cross-variation duplicate ownership is enforced.
        owned_by_variation: list[dict] = []
        for portal, variation, raw_jobs in variation_results:
            excluded_for_portal = excluded_success_keys_by_portal.get(portal, set())
            kept: list[dict] = []
            for raw in raw_jobs:
                keys = [key for key in engine.duplicate_key(raw) if key]
                duplicate_reason = None
                if any(key in seen_keys for key in keys):
                    duplicate_reason = "Duplicate in current search batch"
                elif any(key in excluded_for_portal for key in keys):
                    duplicate_reason = "Already successfully applied for this resume and portal"
                if duplicate_reason:
                    duplicate_jobs.append(
                        {
                            "title": raw.get("title"),
                            "company": raw.get("company"),
                            "job_url": raw.get("job_url"),
                            "reason": duplicate_reason,
                            "search_query_used": variation.query,
                            "search_role": variation.search_role,
                            "role_priority": variation.role_priority,
                        }
                    )
                    continue
                # Easy Apply / quick-apply ONLY may enter the final matched batch. Each portal's
                # own detection sets raw["easy_apply"] (LinkedIn Easy Apply, Dice quick-apply);
                # a non-Easy-Apply or external-site job is never accepted -- and in particular is
                # never used to fill a weighted quota. It is NOT marked seen, so a genuine Easy
                # Apply listing of the same canonical job under another variation can still count.
                if not raw.get("easy_apply"):
                    ineligible_jobs.append(
                        {
                            "title": raw.get("title"),
                            "company": raw.get("company"),
                            "job_url": raw.get("job_url"),
                            "reason": "Not Easy Apply / external application",
                            "search_query_used": variation.query,
                            "search_role": variation.search_role,
                            "role_priority": variation.role_priority,
                        }
                    )
                    continue
                seen_keys.update(keys)
                kept.append(raw)
            owned_by_variation.append({"portal": portal, "variation": variation, "raw": kept, "raw_count": len(raw_jobs)})

        # Weighted soft allocation over the eligible jobs each variation actually owns: the
        # top-five variations get a larger share of the SAME overall target, and any unused
        # budget is reallocated to the highest-priority variations first. The total never
        # exceeds target_count; a variation with few eligible jobs simply contributes fewer --
        # nothing irrelevant is added and relevance/eligibility above are untouched.
        available = [len(entry["raw"]) for entry in owned_by_variation]
        take_per_variation = engine.weighted_take(available, target_count)
        engine.write_debug(
            debug_dir,
            "weighted_allocation.json",
            {
                "target_count": target_count,
                "top_variation_count": engine.TOP_VARIATION_COUNT,
                "top_variation_weight": engine.TOP_VARIATION_WEIGHT,
                "per_variation": [
                    {
                        "variation_index": entry["variation"].variation_index,
                        "query": entry["variation"].query,
                        "portal": entry["portal"],
                        "eligible_available": available[i],
                        "allocated": take_per_variation[i],
                    }
                    for i, entry in enumerate(owned_by_variation)
                ],
            },
        )

        # Pass 2 -- keep each variation's allocated share, in priority order, and persist.
        for entry, take_n in zip(owned_by_variation, take_per_variation):
            portal = entry["portal"]
            variation = entry["variation"]
            before_variation_count = len(jobs)
            for raw in entry["raw"][:take_n]:
                # One reference timestamp for the whole run: every relative posting age in
                # this batch is normalized against the same instant.
                enriched = engine.enrich_raw_job(raw, variation, reference_time=search_timestamp)
                enriched["portal"] = raw.get("portal") or portal
                # The portal's own resolved per-job URL, verbatim.
                enriched["job_url"] = engine.storage_job_url(raw)
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
                        "portal": job.portal,
                        "search_query_used": job.search_query_used,
                        "search_role": job.search_role,
                        "role_priority": job.role_priority,
                        "variation_index": variation.variation_index,
                        "location": job.location,
                    }
                )
            jobs_per_variation.append(
                {
                    "query": variation.query,
                    "location": variation.location,
                    "portal": portal,
                    "search_role": variation.search_role,
                    "role_priority": variation.role_priority,
                    "variation_index": variation.variation_index,
                    "raw_count": entry["raw_count"],
                    "eligible_available": len(entry["raw"]),
                    "allocated": take_n,
                    "unique_added": len(jobs) - before_variation_count,
                }
            )
            if batch is not None:
                _update_search_batch(
                    db,
                    batch,
                    current_search_variation=variation.query,
                    jobs_collected=len(jobs),
                    easy_apply_count=sum(1 for item in jobs if item.easy_apply),
                    duplicates_removed=len(duplicate_jobs),
                    latest_status_message=(
                        f"Collected {len(jobs)} unique jobs; removed {len(duplicate_jobs)} duplicates."
                    ),
                )
        engine.write_debug(debug_dir, "jobs_found.json", jobs_found)
        engine.write_debug(debug_dir, "duplicate_jobs.json", duplicate_jobs)
        engine.write_debug(debug_dir, "ineligible_jobs.json", ineligible_jobs)
        engine.write_debug(debug_dir, "jobs_per_variation.json", jobs_per_variation)

        scorer = MatchScorer(settings)
        if batch is not None:
            _update_search_batch(
                db,
                batch,
                latest_status_message=f"Scoring and ranking {len(jobs)} collected jobs.",
            )
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
                # MERGE, never replace: the portal/enrichment metadata (posted_date,
                # posted_at, Dice job_guid/url_status, apply_url, ...) must survive the
                # reasoning pass. Key sets are disjoint, so reasoning loses nothing either.
                job.job_description_metadata = {**(job.job_description_metadata or {}), **metadata}
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
        if batch is not None:
            _update_search_batch(
                db,
                batch,
                jobs_collected=len(ranked),
                easy_apply_count=sum(1 for job in ranked if job.easy_apply),
                duplicates_removed=len(duplicate_jobs),
                latest_status_message=f"Saved {len(ranked[:target_count])} ranked jobs.",
            )
        engine.write_debug(debug_dir, "jobs_found.json", jobs_found)
        engine.write_debug(debug_dir, "duplicate_jobs.json", duplicate_jobs)
        engine.write_debug(
            debug_dir,
            "search_summary.json",
            {
                "requested_limit": request.limit,
                "target_count": target_count,
                "unique_jobs_found": len(jobs),
                "ranked_jobs_returned": min(len(ranked), target_count),
                "easy_apply_jobs_found": sum(1 for item in jobs if item.easy_apply),
                "duplicate_jobs_skipped": len(duplicate_jobs),
                "search_variations_used": [
                    {
                        "portal": portal,
                        "query": variation.query,
                        "location": variation.location,
                        "search_role": variation.search_role,
                        "role_priority": variation.role_priority,
                    }
                    for portal in portals
                    for variation in selected_variations
                ],
                "jobs_found_per_variation": jobs_per_variation,
                "returned_count": min(len(ranked), target_count),
                "final_job_urls": [job.job_url for job in ranked[:target_count]],
                "reason_if_below_target": (
                    "LinkedIn returned fewer valid unique Easy Apply jobs than requested after duplicate suppression."
                    if len(ranked) < target_count and portal == "linkedin"
                    else "Selected portal(s) returned fewer valid unique jobs than requested after duplicate suppression."
                    if len(ranked) < target_count
                    else None
                ),
            },
        )
        engine.write_debug(
            debug_dir,
            "final_job_urls.json",
            [
                {
                    "rank": index,
                    "title": job.title,
                    "company": job.company,
                    "portal": job.portal,
                    "job_url": job.job_url,
                    "search_role": job.search_role,
                    "role_priority": job.role_priority,
                    "easy_apply": job.easy_apply,
                    "match_score": job.match_score,
                    "priority_score": job.priority_score,
                }
                for index, job in enumerate(ranked[:target_count], start=1)
            ],
        )
        engine.write_debug(
            debug_dir,
            "match_scores.json",
            [
                {
                    "title": job.title,
                    "company": job.company,
                    "portal": job.portal,
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
                    "portal": job.portal,
                    "complexity_score": job.complexity_score,
                    "complexity_level": job.complexity_level,
                    "estimated_steps": job.estimated_steps,
                    "screening_question_count": job.screening_question_count,
                    "required_fields": job.required_fields,
                    "employment_history_required": job.employment_history_required,
                    "application_friction_score": job.application_friction_score,
                    "completion_likelihood_score": job.completion_likelihood_score,
                    "unsupported_field_count": job.unsupported_field_count,
                    "external_form_risk": job.external_form_risk,
                    "manual_review_risk": job.manual_review_risk,
                    "form_provider": job.form_provider,
                    "friction_reasons": job.friction_reasons,
                    "recommended_apply_priority": job.recommended_apply_priority,
                    "apply_priority_reason": job.apply_priority_reason,
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
                    "portal": job.portal,
                    "priority_score": job.priority_score,
                    "priority_reason": job.priority_reason,
                    "completion_likelihood_score": job.completion_likelihood_score,
                    "application_friction_score": job.application_friction_score,
                    "recommended_apply_priority": job.recommended_apply_priority,
                    "apply_priority_reason": job.apply_priority_reason,
                    "search_query_used": job.search_query_used,
                    "search_role": job.search_role,
                    "role_priority": job.role_priority,
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
    portal: str | None = None,
    db: Session = Depends(get_db),
) -> list[JobOpportunity]:
    active = request_portal_or_active(db, portal)
    statement = select(JobOpportunity).order_by(*_job_apply_ordering())
    statement = statement.where(JobOpportunity.portal == active)
    if resume_id:
        statement = statement.where(JobOpportunity.resume_id == resume_id)
        if search_batch_id is None:
            latest_batch = latest_search_batch(db, resume_id, active)
            if latest_batch:
                search_batch_id = latest_batch.id
    if search_batch_id:
        statement = statement.where(JobOpportunity.search_batch_id == search_batch_id)
    return list(db.scalars(statement).all())


@app.get("/jobs/search-batches", response_model=list[SearchBatchOut])
def list_search_batches(
    resume_id: int,
    portal: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    requested = request_portal_or_active(db, portal)
    statement = select(JobSearchBatch).where(JobSearchBatch.resume_id == resume_id)
    statement = statement.where(JobSearchBatch.portal == requested)
    batches = list(
        db.scalars(
            statement.order_by(desc(JobSearchBatch.created_at))
        ).all()
    )
    return [
        _search_batch_out(db, batch)
        for batch in batches
    ]


@app.get("/jobs/search-batches/{batch_id}", response_model=SearchBatchOut)
def get_search_batch(batch_id: str, db: Session = Depends(get_db)) -> dict:
    batch = db.get(JobSearchBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Search batch not found.")
    return _search_batch_out(db, batch)


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
        active = require_active_portal(db, request.portal)
        if resume.portal != active:
            raise HTTPException(status_code=409, detail=f"Resume belongs to {resume.portal.title()}, not {active.title()}.")
        dry_run = settings.auto_apply_dry_run if request.dry_run is None else request.dry_run
        limit = min(request.limit, settings.max_applications_per_run)
        search_batch_id = request.search_batch_id
        with recorder.stage("job_queue_loading"):
            if search_batch_id:
                batch = db.get(JobSearchBatch, search_batch_id)
                if not batch or batch.resume_id != resume.id:
                    raise HTTPException(status_code=404, detail="Search batch not found for this resume.")
                if batch.portal != active:
                    raise HTTPException(status_code=409, detail=f"Search batch belongs to {batch.portal.title()}, but active portal is {active.title()}.")
            else:
                batch = latest_search_batch(db, resume.id, active)
                search_batch_id = batch.id if batch else None
            job_statement = (
                select(JobOpportunity)
                .where(JobOpportunity.resume_id == resume.id)
                .where(JobOpportunity.status.in_([ApplicationStatus.MATCHED, ApplicationStatus.QUEUED]))
            )
            if search_batch_id:
                job_statement = job_statement.where(JobOpportunity.search_batch_id == search_batch_id)
            job_statement = job_statement.where(JobOpportunity.portal == active)
            jobs = list(
                db.scalars(
                    job_statement.order_by(*_job_apply_ordering()).limit(limit)
                ).all()
            )
        automation = LinkedInAutomation(settings)
        records: list[ApplicationRecord] = []
        with recorder.stage("candidate_profile_loading"):
            candidate_profile = default_candidate_profile(db, active)
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


@app.post("/applications/apply-top/start", response_model=ApplyTopBatchOut)
def start_apply_top_batch(
    request: ApplyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ApplyTopBatch:
    # Refuse a second batch BEFORE creating a row, so a rejected request leaves no orphan
    # batch behind and the active one is never disturbed.
    controller = get_automation_controller()
    if controller.is_active():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Apply batch {controller.active_batch_id} is already {controller.state}. "
                "Wait for it to finish, or stop it first."
            ),
        )
    batch = _create_apply_top_batch(request, db)
    _claim_automation_slot(batch.id)
    background_tasks.add_task(_run_apply_top_batch, batch.id)
    return batch


@app.post("/applications/apply-top/{batch_id}/stop", response_model=ApplyTopBatchOut)
def stop_apply_top_batch(batch_id: str, db: Session = Depends(get_db)) -> ApplyTopBatch:
    """Ask the active apply batch to stop gracefully.

    Returns immediately: this only RAISES the flag. The worker notices it at its next
    checkpoint, finishes whatever browser action is in flight, records the remaining jobs as
    stopped and then ends the batch. No process is ended and no page is closed from here."""
    batch = db.get(ApplyTopBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Apply batch not found.")
    if batch.status in {"completed", "failed", BATCH_STATUS_STOPPED}:
        raise HTTPException(status_code=409, detail=f"Apply batch is already {batch.status}.")
    if not get_automation_controller().request_stop(batch_id):
        # Either it is not the active batch, or a stop is already in flight. Both are fine to
        # report as the current state rather than as an error the user must act on.
        if get_automation_controller().active_batch_id != batch_id:
            raise HTTPException(status_code=409, detail="Apply batch is not the active automation run.")
    batch.status = BATCH_STATUS_STOPPING
    batch.latest_status_message = "Stopping after the current step; no further job will be submitted."
    db.add(batch)
    db.commit()
    db.refresh(batch)
    _write_apply_top_debug(batch)
    return batch


@app.post("/applications/apply-top/{batch_id}/resume", response_model=ApplyTopBatchOut)
def resume_apply_top_batch(
    batch_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ApplyTopBatch:
    """Continue an interrupted apply batch from its first unprocessed job.

    This re-enters the EXISTING batch: no row is created, no search is re-run, no job list is
    regenerated and nothing already processed is revisited. Every validation lives in
    _resume_blocker, so a batch the dashboard offers is a batch this accepts."""
    controller = get_automation_controller()
    if controller.is_active():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Apply batch {controller.active_batch_id} is already {controller.state}. "
                "Wait for it to finish, or stop it first."
            ),
        )
    batch = db.get(ApplyTopBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Apply batch not found.")
    blocker = _resume_blocker(db, batch)
    if blocker:
        # 409, not 404: the batch exists, it just cannot be continued. The dashboard drops the
        # Continue button and offers Apply Jobs again on this reply.
        raise HTTPException(status_code=409, detail=blocker)
    _claim_automation_slot(batch.id)
    background_tasks.add_task(_run_apply_top_batch, batch.id, True)
    return _decorate_resumability(db, batch)


@app.get("/applications/automation/state", response_model=AutomationStateOut)
def get_automation_state() -> dict:
    """Whether an apply batch is running or stopping, straight from the backend.

    The Stop button is driven by this, not by frontend state, so a page reload during a run
    still shows the right control."""
    return get_automation_controller().snapshot()


@app.get("/applications/apply-top/batches/{batch_id}", response_model=ApplyTopBatchOut)
def get_apply_top_batch(batch_id: str, db: Session = Depends(get_db)) -> ApplyTopBatch:
    batch = db.get(ApplyTopBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Apply Top 10 batch not found.")
    return _decorate_resumability(db, batch)


@app.get("/applications/apply-top/latest", response_model=ApplyTopBatchOut | None)
def get_latest_apply_top_batch(
    resume_id: int,
    portal: str | None = None,
    db: Session = Depends(get_db),
) -> ApplyTopBatch | None:
    active = request_portal_or_active(db, portal)
    statement = (
        select(ApplyTopBatch)
        .where(ApplyTopBatch.resume_id == resume_id)
        .where(ApplyTopBatch.portal == active)
    )
    batch = db.scalar(statement.order_by(desc(ApplyTopBatch.created_at)).limit(1))
    # The dashboard decides between Apply Jobs and Continue Automation from this reply, so the
    # verdict travels with the batch rather than being re-derived in the browser.
    return _decorate_resumability(db, batch)


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
        active = get_active_portal(db)
        if resume.portal != active:
            raise HTTPException(status_code=409, detail=f"Resume belongs to {resume.portal.title()}, not {active.title()}.")
        with recorder.stage("job_loading"):
            job = db.get(JobOpportunity, request.job_id)
        if not job or job.resume_id != resume.id:
            raise HTTPException(status_code=404, detail="Selected job was not found for this resume.")
        if job.portal != active:
            raise HTTPException(status_code=409, detail=f"Selected job belongs to {job.portal.title()}, but active portal is {active.title()}.")
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


def _create_apply_top_batch(request: ApplyRequest, db: Session) -> ApplyTopBatch:
    resume = db.get(ResumeProfile, request.resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")
    active = require_active_portal(db, request.portal)
    if resume.portal != active:
        raise HTTPException(status_code=409, detail=f"Resume belongs to {resume.portal.title()}, not {active.title()}.")
    search_batch_id = request.search_batch_id
    selected_search_batch = None
    if search_batch_id:
        selected_search_batch = db.get(JobSearchBatch, search_batch_id)
        if not selected_search_batch or selected_search_batch.resume_id != resume.id:
            raise HTTPException(status_code=404, detail="Search batch not found for this resume.")
        if selected_search_batch.portal != active:
            raise HTTPException(status_code=409, detail=f"Search batch belongs to {selected_search_batch.portal.title()}, but active portal is {active.title()}.")
    else:
        latest_batch = latest_search_batch(db, resume.id, active)
        search_batch_id = latest_batch.id if latest_batch else None
        selected_search_batch = latest_batch
    limit = min(request.limit, settings.max_applications_per_run)
    portal = active
    dry_run = settings.auto_apply_dry_run if request.dry_run is None else request.dry_run
    screening_setting = default_screening_setting(db)
    screening_mode = (request.screening_mode or screening_setting.mode or "safe").lower()
    total_count = (
        db.scalar(
            select(func.count()).select_from(
                _apply_top_jobs_statement(resume.id, search_batch_id, portal=portal).subquery()
            )
        )
        or 0
    )
    total_count = min(total_count, limit)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    batch_id = str(uuid4())
    debug_dir = settings.apply_top_debug_dir / timestamp
    debug_dir.mkdir(parents=True, exist_ok=True)
    apply_batch = ApplyTopBatch(
        id=batch_id,
        resume_id=resume.id,
        search_batch_id=search_batch_id,
        portal=portal,
        status="queued",
        dry_run=dry_run,
        screening_mode=screening_mode,
        limit=limit,
        total_count=total_count,
        latest_status_message=f"Queued Apply Top {limit} dry run." if dry_run else f"Queued Apply Top {limit} run.",
        debug_path=str(debug_dir),
        job_results=[],
    )
    db.add(apply_batch)
    db.commit()
    db.refresh(apply_batch)
    _write_apply_top_debug(apply_batch)
    return apply_batch


def _claim_automation_slot(batch_id: str) -> None:
    """Reserve the single automation slot, or refuse the request.

    Claimed synchronously in the HTTP handler, BEFORE the background worker is queued, so two
    quick Apply Jobs clicks cannot both reach the browser. The slot stays claimed while a batch
    is stopping, so a stop can never race a fresh batch into the same portal session."""
    try:
        get_automation_controller().start(batch_id)
    except BatchAlreadyActive as active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Apply batch {active.batch_id} is already {active.state}. "
                "Wait for it to finish, or stop it first."
            ),
        ) from None


def _run_apply_top_batch(batch_id: str, resuming: bool = False) -> None:
    controller = get_automation_controller()
    # The slot was claimed synchronously in _create_apply_top_batch, so a second Apply Jobs
    # request is refused before it can ever reach this worker. Re-read the token here rather
    # than passing it in: the HTTP thread that services Stop mutates the same object.
    token = controller.token_for(batch_id)
    with database.SessionLocal() as db:
        batch = db.get(ApplyTopBatch, batch_id)
        if not batch:
            logger.error("Apply Top batch %s was not found.", batch_id)
            controller.finish(batch_id)
            return
        try:
            batch.status = "running"
            # started_at records when the batch first began, so a resume does not erase it.
            if not resuming or batch.started_at is None:
                batch.started_at = datetime.utcnow()
            batch.finished_at = None
            batch.error_message = None
            batch.latest_status_message = (
                "Continuing the interrupted batch." if resuming else "Loading resume and job queue."
            )
            db.add(batch)
            db.commit()
            _write_apply_top_debug(batch)

            resume = db.get(ResumeProfile, batch.resume_id)
            if not resume:
                raise RuntimeError("Resume not found.")

            # Jobs already processed keep their results and are never revisited; the `stopped`
            # placeholders written by the previous stop are dropped, because those jobs never
            # ran and are about to be attempted for real.
            done = 0
            if resuming:
                kept = _processed_results(batch)
                if len(kept) != len(batch.job_results or []):
                    _recount_apply_top(batch, kept)
                    db.add(batch)
                    db.commit()
                done = len(kept)

            jobs = _apply_top_remaining_jobs(db, batch, resume)
            # The original plan's size: what was already done plus what is left to do.
            batch.total_count = done + len(jobs)
            batch.current_index = done
            db.add(batch)
            db.commit()

            automation = LinkedInAutomation(settings)
            candidate_profile = default_candidate_profile(db)
            screening_setting = default_screening_setting(db)
            screening_context = {
                "mode": batch.screening_mode or screening_setting.mode or "safe",
                "notice_period_default": screening_setting.notice_period_default,
                "saved_answers": screening_answers_for_candidate(db, candidate_profile),
                "employment_history": employment_history_payload(
                    ensure_employment_history_for_resume(db, candidate_profile, resume)
                ),
            }
            stopped = False
            # `position` walks the remaining jobs; `index` is the job's place in the batch as a
            # whole, so a resumed run continues 28, 29, 30 rather than restarting at 1.
            for position, job in enumerate(jobs, start=1):
                index = done + position
                # Checkpoint 1: before starting the next job. Everything already applied for is
                # keeping its result; this job and every job after it are recorded as stopped.
                if token is not None and token.cancelled:
                    stopped = True
                    _record_stopped_jobs(db, batch.id, jobs[position - 1:], index)
                    break
                started = datetime.utcnow()
                batch.current_index = index
                batch.current_job_id = job.id
                batch.current_title = job.title
                batch.current_company = job.company
                batch.latest_status_message = f"Processing {index}/{batch.total_count}: {job.title} at {job.company}"
                db.add(batch)
                db.commit()
                _write_apply_top_debug(batch)
                try:
                    record = _process_application(
                        db,
                        automation,
                        job,
                        resume,
                        candidate_profile,
                        batch.dry_run,
                        capture_evidence=batch.dry_run,
                        screening_context=screening_context,
                        cancellation=token,
                    )
                    result = _apply_top_job_result(index, job, record, started)
                except AutomationCancelled as cancelled:
                    # A checkpoint fired inside the portal adapter. This job was interrupted,
                    # not failed: no application was submitted and no history row was written.
                    logger.info("Apply Top batch %s stopped at %s", batch.id, cancelled.checkpoint)
                    stopped = True
                    _record_stopped_jobs(db, batch.id, jobs[position - 1:], index,
                                         checkpoint=cancelled.checkpoint)
                    break
                except Exception as exc:
                    logger.exception("Apply Top batch %s failed on job %s", batch.id, job.id)
                    result = _apply_top_failure_result(index, job, started, exc)
                _append_apply_top_result(db, batch.id, result)

            batch = db.get(ApplyTopBatch, batch.id)
            if batch:
                # A stopped batch is a finished batch with its summary intact -- completed,
                # stopped, skipped and failed counts all survive, and so does every application
                # that was already submitted.
                batch.status = BATCH_STATUS_STOPPED if stopped else "completed"
                batch.finished_at = datetime.utcnow()
                batch.current_job_id = None
                batch.current_title = None
                batch.current_company = None
                batch.latest_status_message = _apply_top_summary_message(batch, stopped=stopped)
                db.add(batch)
                db.commit()
                _write_apply_top_debug(batch)
        except Exception as exc:
            logger.exception("Apply Top batch %s failed.", batch_id)
            batch = db.get(ApplyTopBatch, batch_id)
            if batch:
                batch.status = "failed"
                batch.error_message = str(exc)
                batch.finished_at = datetime.utcnow()
                batch.latest_status_message = f"Batch failed: {exc}"
                db.add(batch)
                db.commit()
                _write_apply_top_debug(batch)
        finally:
            # Always release the automation slot, or every later Apply Jobs run is blocked.
            controller.finish(batch_id)


def _apply_top_jobs_statement(resume_id: int, search_batch_id: str | None, portal: str | None = None):
    statement = (
        select(JobOpportunity)
        .where(JobOpportunity.resume_id == resume_id)
        .where(JobOpportunity.status.in_([ApplicationStatus.MATCHED, ApplicationStatus.QUEUED]))
    )
    if search_batch_id:
        statement = statement.where(JobOpportunity.search_batch_id == search_batch_id)
    requested = normalize_portal(portal) if portal else None
    if requested:
        statement = statement.where(JobOpportunity.portal == requested)
    return statement.order_by(*_job_apply_ordering())


def _job_apply_ordering():
    # Skill-priority GROUPED order for the completed batch. Relevance and eligibility decide
    # whether a job ENTERS the matched set (collection/scoring, unchanged); the search variation
    # that discovered it then decides its GROUP, and posted time orders it only WITHIN that group.
    #
    # 1. search variation index ascending -- variation 1's jobs, then variation 2's, ... so each
    #    variation's rows stay contiguous. Read from job_description_metadata.variation_index
    #    (JSON, no schema change); rows without it (legacy batches) sort last.
    # 2-3. freshness_score descending WITHIN the group: it is the SQL-sortable proxy for
    #    normalized posted_at (strictly age-monotone; unknown/unparseable at exactly 0, below the
    #    confirmed floor of 5), so "confirmed newest -> confirmed oldest -> unknown" falls out of
    #    one descending key. Posting time is NEVER compared across different variations.
    # 4-6. match_score, then priority_score, then id break ties only within one variation+time.
    #    id is a deterministic final tie-break, not a source of ordering -- variation and posted
    #    time dominate, so database insertion order has no influence.
    variation_index = cast(
        JobOpportunity.job_description_metadata["variation_index"].astext, Integer
    )
    return (
        asc(variation_index).nullslast(),
        desc(JobOpportunity.freshness_score),
        desc(JobOpportunity.match_score),
        desc(JobOpportunity.priority_score),
        asc(JobOpportunity.id),
    )


def _apply_top_job_result(index: int, job: JobOpportunity, record: ApplicationRecord, started: datetime) -> dict:
    duration = round((datetime.utcnow() - started).total_seconds(), 2)
    status = _batch_job_status(record)
    return {
        "job_index": index,
        "job_id": job.id,
        "portal": job.portal,
        "title": job.title,
        "company": job.company,
        "status": status,
        "application_record_id": record.id,
        "current_step": record.current_step,
        "duration": duration,
        "error": record.error_message,
        "evidence_path": record.evidence_path,
    }


def _apply_top_failure_result(index: int, job: JobOpportunity, started: datetime, exc: Exception) -> dict:
    duration = round((datetime.utcnow() - started).total_seconds(), 2)
    message = str(exc)
    status = "timed_out" if "timeout" in message.lower() or "timed out" in message.lower() else "failed"
    return {
        "job_index": index,
        "job_id": job.id,
        "portal": job.portal,
        "title": job.title,
        "company": job.company,
        "status": status,
        "application_record_id": None,
        "current_step": status,
        "duration": duration,
        "error": message,
        "evidence_path": None,
    }


def _batch_job_status(record: ApplicationRecord) -> str:
    combined = f"{record.current_step or ''} {record.error_message or ''}".lower()
    if "timeout" in combined or "timed out" in combined:
        return "timed_out"
    if record.status == ApplicationStatus.SKIPPED:
        # Legacy rows only: successful dry runs are now stored as DRY_RUN_READY, but
        # records written before that fix are still SKIPPED with a dry-run-complete step.
        if record.current_step in DRY_RUN_COMPLETE_STEPS:
            return "reached_review"
        return "skipped"
    if record.status == ApplicationStatus.DRY_RUN_READY:
        # Dry run reached the submit/review stage without submitting.
        return "reached_review"
    if record.status == ApplicationStatus.UNSUPPORTED_FORM:
        # Form could not be automated safely; surface it for manual review.
        return "needs_review"
    if record.status == ApplicationStatus.NEEDS_REVIEW:
        return "needs_review"
    if record.status == ApplicationStatus.FAILED:
        return "failed"
    if record.status == ApplicationStatus.APPLIED:
        return "applied"
    return str(record.status.value if hasattr(record.status, "value") else record.status)


def _recount_apply_top(batch: ApplyTopBatch, results: list[dict]) -> None:
    """Re-derive every counter from the job_results list.

    One place, so appending a result and dropping the stopped placeholders on resume can never
    disagree about what the counters mean."""
    batch.job_results = results
    batch.completed_count = len(results)
    batch.skipped_count = sum(1 for item in results if item.get("status") in {"skipped", "reached_review"})
    batch.failed_count = sum(1 for item in results if item.get("status") == "failed")
    # `stopped` is deliberately absent from failed/needs-review/timed-out: stopping is a user
    # action, not an outcome of the application. It is reported on its own (stopped_count).
    batch.needs_review_count = sum(1 for item in results if item.get("status") == "needs_review")
    batch.timed_out_count = sum(1 for item in results if item.get("status") == "timed_out")


def _append_apply_top_result(db: Session, batch_id: str, result: dict) -> None:
    batch = db.get(ApplyTopBatch, batch_id)
    if not batch:
        return
    results = list(batch.job_results or [])
    results.append(result)
    _recount_apply_top(batch, results)
    batch.latest_status_message = f"Completed {batch.completed_count}/{batch.total_count}: {result['title']} - {result['status']}"
    db.add(batch)
    db.commit()
    _write_apply_top_debug(batch)


# ======================================================================================
# Continue Automation
#
# Resuming re-enters the SAME batch row. No batch is created, no search is re-run, nothing is
# re-ranked or re-allocated: _apply_top_jobs_statement is deterministic SQL over columns that
# were persisted when the jobs were first collected (variation_index, freshness, match and
# priority scores, id), so running it again restores the original order, the original
# allocation and the original duplicate ownership rather than recomputing them.
#
# What a resume must NOT do is offer a job that has already been processed. Job status alone
# cannot answer that: _process_application leaves a job MATCHED in dry run, so a status-only
# filter would re-apply to every dry-run job in the batch. job_results is the authoritative
# per-job record and is used instead.
# ======================================================================================

def _processed_results(batch: ApplyTopBatch) -> list[dict]:
    """Results for jobs that genuinely ran. A `stopped` entry is a placeholder for a job that
    never started, so it is not progress and must not block a later attempt."""
    return [item for item in (batch.job_results or []) if item.get("status") != JOB_RESULT_STOPPED]


def _processed_job_ids(batch: ApplyTopBatch) -> set[int]:
    return {
        item["job_id"] for item in _processed_results(batch)
        if item.get("job_id") is not None
    }


def _apply_top_remaining_jobs(
    db: Session, batch: ApplyTopBatch, resume: ResumeProfile
) -> list[JobOpportunity]:
    """The jobs this batch still has to process, in the batch's original order.

    For a batch that has never run this is exactly today's query and today's result -- nothing
    has been processed, so nothing is excluded and the whole limit is available."""
    processed = _processed_job_ids(batch)
    # The limit is the batch's budget, not a per-attempt budget: a batch of 20 that stopped
    # after 5 has 15 left, never another 20.
    remaining_budget = max(0, batch.limit - len(_processed_results(batch)))
    if remaining_budget == 0:
        return []
    statement = _apply_top_jobs_statement(resume.id, batch.search_batch_id, portal=batch.portal)
    if processed:
        statement = statement.where(JobOpportunity.id.notin_(processed))
    return list(db.scalars(statement.limit(remaining_budget)).all())


def _resume_blocker(db: Session, batch: ApplyTopBatch) -> str | None:
    """Why this batch cannot be continued, or None if it can.

    Every validation the feature promises lives here, so the button, the endpoint and the
    tests all agree on one definition of "resumable"."""
    if batch.status not in RESUMABLE_BATCH_STATUSES:
        return f"This batch is {batch.status}, not an interrupted run."
    resume = db.get(ResumeProfile, batch.resume_id)
    if not resume:
        return "The resume this batch was built from no longer exists."
    # Only the newest batch may be continued: an older interrupted batch was superseded the
    # moment a newer one was started, and resuming it would interleave two runs.
    newest = db.scalar(
        select(ApplyTopBatch)
        .where(ApplyTopBatch.resume_id == batch.resume_id)
        .where(ApplyTopBatch.portal == batch.portal)
        .order_by(desc(ApplyTopBatch.created_at))
        .limit(1)
    )
    if newest is not None and newest.id != batch.id:
        return "A newer apply batch has replaced this one."
    # A new Find Matches produces a different result set, and this batch's order, allocation
    # and duplicate ownership all belong to the old one. Continuing across that boundary would
    # apply the old plan to new jobs, so the batch is retired instead.
    latest_search = latest_search_batch(db, resume.id, batch.portal)
    latest_search_id = latest_search.id if latest_search else None
    if batch.search_batch_id != latest_search_id:
        return "A newer job search has replaced the one this batch was built from."
    if not _apply_top_remaining_jobs(db, batch, resume):
        return "No eligible jobs remain in this batch."
    return None


def _decorate_resumability(db: Session, batch: ApplyTopBatch | None) -> ApplyTopBatch | None:
    """Attach the resume verdict to a batch on its way out to the dashboard.

    Set as plain instance attributes rather than columns: they are derived from data the batch
    already holds, so storing them would create a second source of truth that could go stale."""
    if batch is None:
        return None
    blocker = _resume_blocker(db, batch)
    batch.resumable = blocker is None
    batch.resume_blocked_reason = blocker
    resume = db.get(ResumeProfile, batch.resume_id)
    batch.remaining_count = (
        len(_apply_top_remaining_jobs(db, batch, resume)) if resume is not None else 0
    )
    return batch


def _adopt_interrupted_batches() -> None:
    """Mark batches whose worker never finished as interrupted.

    Runs at startup. A fresh process cannot have inherited a live worker, so anything still
    recorded as queued, running or stopping was cut short by the application closing, a crash
    or the machine going away -- exactly the cases Continue Automation exists for. Only the
    batch's own lifecycle field changes; job results, counters and history are untouched."""
    with database.SessionLocal() as db:
        orphans = list(
            db.scalars(
                select(ApplyTopBatch).where(
                    ApplyTopBatch.status.in_(["queued", "running", BATCH_STATUS_STOPPING])
                )
            ).all()
        )
        for batch in orphans:
            batch.status = BATCH_STATUS_INTERRUPTED
            batch.finished_at = batch.finished_at or datetime.utcnow()
            batch.current_job_id = None
            batch.current_title = None
            batch.current_company = None
            batch.latest_status_message = (
                "Automation was interrupted before it finished. Continue Automation resumes it "
                "from the next unprocessed job."
            )
            db.add(batch)
        if orphans:
            db.commit()
            logger.info("Marked %d unfinished apply batch(es) as interrupted.", len(orphans))


def _record_stopped_jobs(
    db: Session,
    batch_id: str,
    jobs: list[JobOpportunity],
    start_index: int,
    checkpoint: str = "before_next_job",
) -> None:
    """Record the interrupted job and every job after it as STOPPED.

    Requirement: a job the user stopped is not a failure. It gets a batch result so the summary
    stays honest about what did and did not run, but no ApplicationRecord and no change to the
    job's own status -- so it is still MATCHED and still eligible for a later batch, and
    Application History is untouched."""
    for offset, job in enumerate(jobs):
        _append_apply_top_result(
            db,
            batch_id,
            {
                "job_index": start_index + offset,
                "job_id": job.id,
                "portal": job.portal,
                "title": job.title,
                "company": job.company,
                "status": JOB_RESULT_STOPPED,
                "application_record_id": None,
                "current_step": "stopped_by_user",
                "duration": 0.0,
                "error": None,
                "evidence_path": None,
                "stop_checkpoint": checkpoint if offset == 0 else "not_started",
            },
        )


def _apply_top_summary_message(batch: ApplyTopBatch, stopped: bool = False) -> str:
    lead = "Batch stopped by user" if stopped else "Batch complete"
    message = (
        f"{lead}: {batch.completed_count}/{batch.total_count} processed, "
        f"{batch.needs_review_count} needs review, {batch.failed_count} failed, "
        f"{batch.timed_out_count} timed out."
    )
    if batch.stopped_count:
        message += f" {batch.stopped_count} stopped."
    return message


def _write_apply_top_debug(batch: ApplyTopBatch) -> None:
    if not batch.debug_path:
        return
    debug_dir = Path(batch.debug_path)
    debug_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_id": batch.id,
        "resume_id": batch.resume_id,
        "search_batch_id": batch.search_batch_id,
        "portal": batch.portal,
        "started_at": batch.started_at,
        "finished_at": batch.finished_at,
        "status": batch.status,
        "dry_run": batch.dry_run,
        "screening_mode": batch.screening_mode,
        "limit": batch.limit,
        "total_count": batch.total_count,
        "current_job_number": batch.current_index,
        "current_job_id": batch.current_job_id,
        "current_title": batch.current_title,
        "current_company": batch.current_company,
        "completed_count": batch.completed_count,
        "skipped_count": batch.skipped_count,
        "failed_count": batch.failed_count,
        "needs_review_count": batch.needs_review_count,
        "timed_out_count": batch.timed_out_count,
        "stopped_count": batch.stopped_count,
        "latest_status_message": batch.latest_status_message,
        "error_message": batch.error_message,
        "jobs": batch.job_results or [],
        "updated_at": datetime.utcnow().isoformat(),
    }
    (debug_dir / "batch_status.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _process_application(
    db: Session,
    automation: LinkedInAutomation,
    job: JobOpportunity,
    resume: ResumeProfile,
    candidate_profile: CandidateProfile | None,
    dry_run: bool,
    capture_evidence: bool,
    screening_context: dict | None = None,
    cancellation=None,
) -> ApplicationRecord:
    try:
        if job.portal == "dice":
            result = dice_adapter().apply_to_job(
                job,
                resume,
                candidate_profile,
                dry_run=dry_run,
                capture_evidence=capture_evidence,
                screening_context=screening_context,
                cancellation=cancellation,
            )
        else:
            result = automation.apply_to_job(
                job,
                resume,
                candidate_profile,
                dry_run=dry_run,
                capture_evidence=capture_evidence,
                screening_context=screening_context,
                cancellation=cancellation,
            )
        job.status = ApplicationStatus.MATCHED if dry_run else result.status
        if candidate_profile is not None:
            candidate_profile.missing_required_after_apply = result.missing_contact_fields or []
            db.add(candidate_profile)
            _save_skill_experience_estimates(db, candidate_profile, resume, result)
        _save_form_learning_observations(db, job, result)
        record = _record_application_outcome(
            db,
            job,
            portal=job.portal,
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
    except AutomationCancelled:
        # Control flow, not a failure: the user stopped the run. Nothing is written to
        # Application History and the job keeps its current status, so it stays eligible for a
        # later batch. The caller records it as stopped.
        raise
    except Exception as exc:
        logger.exception("Application failed for job %s", job.id)
        job.status = ApplicationStatus.FAILED
        record = _record_application_outcome(
            db,
            job,
            portal=job.portal,
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


def _record_application_outcome(db: Session, job: JobOpportunity, **fields) -> ApplicationRecord:
    """Persist the outcome of an apply attempt as the job's single history record.

    Application History is permanent, so every attempt is recorded -- but a job gets ONE
    row, not one per attempt. Re-applying the same job updates that row in place instead of
    inserting a duplicate history entry.

    A success is authoritative and can never be erased by a later attempt: the incoming
    outcome only overwrites the stored one when it ranks at least as high (see
    ``outcome_supersedes``). So a live apply upgrades a submit-ready dry run, but a later
    failure/skip/needs-review leaves the recorded success -- and its evidence -- intact.
    ``attempt_count`` still increments either way, so a superseded attempt is counted rather
    than silently discarded.
    """
    existing = db.scalars(
        select(ApplicationRecord)
        .where(ApplicationRecord.job_id == job.id)
        .order_by(desc(ApplicationRecord.application_date), desc(ApplicationRecord.id))
        .limit(1)
    ).first()
    if existing is None:
        return ApplicationRecord(job_id=job.id, **fields)
    if outcome_supersedes(fields.get("status"), existing.status):
        # The new attempt replaces the stored one, so the row must describe THAT attempt and
        # nothing else. Clear every attempt-specific column the new attempt does not write,
        # otherwise it inherits the previous attempt's evidence/reasoning/upload status.
        reset_attempt_specific_fields(existing, keep=fields.keys())
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.application_date = datetime.utcnow()
    # Otherwise the stored outcome outranks this attempt (e.g. FAILED after DRY_RUN_READY):
    # keep the successful status, evidence, reasoning and original date untouched, so the
    # job stays in Application History.
    existing.attempt_count = (existing.attempt_count or 0) + 1
    return existing


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
        job_portal = normalize_portal(getattr(job, "portal", None))
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
                portal=job_portal,
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
    # All confidently answered screening decisions (any stage) are surfaced for
    # dashboard visibility, not only reasoning-engine ones. The reasoning summary
    # fields still prefer a genuine reasoning event when one exists.
    answered = [
        item
        for item in (result.screening_answers or [])
        if item.get("selected_answer") is not None
    ]
    if not events and not answered:
        return {
            "reasoning_type": None,
            "reasoning_summary": None,
            "confidence_score": None,
            "evidence_sources": [],
        }
    primary = events[0] if events else answered[0]
    return {
        "reasoning_type": primary.get("reasoning_type"),
        "reasoning_summary": primary.get("reasoning_summary") or primary.get("reason"),
        "confidence_score": primary.get("confidence"),
        "evidence_sources": [
            {
                "question": event.get("question_text"),
                "answer": event.get("selected_answer"),
                "source": event.get("source"),
                "confidence": event.get("confidence"),
                "reasoning": event.get("reason") or event.get("reasoning_summary"),
                "category": event.get("category"),
                "decision_stage": event.get("decision_stage"),
                "reasoning_type": event.get("reasoning_type"),
                "evidence": (event.get("evidence") or {}).get("evidence_used", []),
            }
            for event in (answered or events)
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
    portal: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    # Application History is PERMANENT history, not batch data. It is scoped by exactly two
    # things -- resume fingerprint family + portal -- and contains only successful runs
    # (APPLIED live submits and DRY_RUN_READY submit-ready dry runs).
    #
    # `search_batch_id` is accepted for API compatibility (both dashboard.py and React still
    # send the selected batch) but is deliberately NOT applied: narrowing history to the
    # currently selected search batch is what made earlier runs disappear after a new search
    # or a resume re-upload.
    #
    # The response reads job fields (title, company, job_url, portal) for every row. The
    # join to JobOpportunity is needed for filtering anyway, so `contains_eager` reuses THAT
    # join to populate ApplicationRecord.job from the same result rows -- no second join, and
    # no lazy SELECT per record. Without it this endpoint was one query per history row.
    active = request_portal_or_active(db, portal)
    statement = (
        select(ApplicationRecord)
        .join(ApplicationRecord.job)
        .options(contains_eager(ApplicationRecord.job))
        .where(JobOpportunity.portal == active)
        .where(ApplicationRecord.status.in_(SUCCESS_APPLICATION_STATUSES))
    )
    if resume_id:
        # Resolve the resume to its fingerprint family so a re-uploaded resume (which gets
        # a new id) still shows the same portal-specific history. Portal isolation is
        # preserved by the JobOpportunity.portal filter above.
        family_ids = resume_family_ids_for(db, resume_id)
        statement = statement.where(JobOpportunity.resume_id.in_(family_ids))
    records = list(
        db.scalars(
            statement.order_by(desc(ApplicationRecord.application_date), desc(ApplicationRecord.id))
        ).all()
    )
    # One history row per job: newest record wins. New records are upserted per job, so this
    # only collapses duplicates written before that change.
    seen_job_ids: set[int] = set()
    deduped: list[ApplicationRecord] = []
    for record in records:
        if record.job_id in seen_job_ids:
            continue
        seen_job_ids.add(record.job_id)
        deduped.append(record)
    records = deduped
    return [
        {
            "id": record.id,
            "job_id": record.job_id,
            "portal": record.portal,
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
            "job_portal": record.job.portal,
        }
        for record in records
    ]


def _empty_dashboard_summary() -> dict:
    """The zeroed counters a clean active session shows."""
    return {
        "matched_jobs": 0,
        "easy_apply_jobs": 0,
        "applied_jobs": 0,
        "needs_review": 0,
        "failed_jobs": 0,
        "skipped_jobs": 0,
    }


@app.get("/dashboard/summary")
def dashboard_summary(
    resume_id: int | None = None,
    search_batch_id: str | None = None,
    portal: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    active = request_portal_or_active(db, portal)
    # A clean session (no active resume) summarises nothing: never fall back to counting
    # every stored job for the portal, and never adopt the newest batch as the active one.
    # The caller states which resume/batch it is looking at; without that, counters are zero.
    if not resume_id or not search_batch_id:
        return _empty_dashboard_summary()
    job_statement = (
        select(JobOpportunity)
        .where(JobOpportunity.portal == active)
        .where(JobOpportunity.resume_id == resume_id)
        .where(JobOpportunity.search_batch_id == search_batch_id)
    )
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


# --- Web interface -------------------------------------------------------------------------
# Registered LAST, after every endpoint above, so the client-side-routing fallback can only
# ever match a path that no API route claimed. In development this installs the "/api" prefix
# rewrite only and leaves the UI to the Vite dev server; in a packaged build it also serves
# frontend/dist, which is what removes the Node.js runtime requirement.
frontend_mount = mount_frontend(
    app,
    dist_dir=settings.frontend_dist_dir,
    enabled=settings.should_serve_frontend(),
)
