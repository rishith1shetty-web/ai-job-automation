from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.models import ApplicationStatus, CandidateProfile, JobOpportunity, ResumeProfile
from app.services.automation_control import CancellationToken
from app.services.find_matches import SearchVariation


@dataclass
class PortalJobResult:
    title: str
    company: str
    location: str | None
    job_url: str
    description: str = ""
    easy_apply: bool = False
    apply_url: str | None = None
    form_provider: str | None = None
    metadata: dict = field(default_factory=dict)

    def as_raw_job(self) -> dict:
        return {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "job_url": self.job_url,
            "description": self.description,
            "easy_apply": self.easy_apply,
            "apply_url": self.apply_url,
            "form_provider": self.form_provider,
            "job_description_metadata": self.metadata,
        }


@dataclass
class PortalApplyResult:
    status: ApplicationStatus
    current_step: str
    message: str
    evidence_path: str | None = None
    log: list[dict] = field(default_factory=list)


class PortalAdapter(Protocol):
    portal_name: str

    def connect(self) -> dict:
        ...

    def is_connected(self) -> bool:
        ...

    def disconnect(self) -> None:
        ...

    def search_jobs_many(
        self,
        resume: ResumeProfile,
        variations: list[SearchVariation],
        limit_per_query: int,
    ) -> list[tuple[SearchVariation, list[dict]]]:
        ...

    def apply_to_job(
        self,
        job: JobOpportunity,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        *,
        dry_run: bool,
        capture_evidence: bool,
        screening_context: dict | None = None,
        # Stop Automation: the active batch's cancellation token. Every adapter must accept it
        # -- an adapter that silently rejected it would have its TypeError recorded as a FAILED
        # application. None means nothing can cancel this run.
        cancellation: "CancellationToken | None" = None,
    ):
        ...
