from __future__ import annotations

from app.config import Settings
from app.models import CandidateProfile, JobOpportunity, ResumeProfile
from app.services.find_matches import SearchVariation
from app.services.linkedin import LinkedInAutomation


class LinkedInPortalAdapter:
    portal_name = "linkedin"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.automation = LinkedInAutomation(settings)

    def connect(self) -> dict:
        self.automation.connect()
        return {"status": "connected", "portal": self.portal_name, "storage_state": str(self.settings.linkedin_storage_state)}

    def is_connected(self) -> bool:
        return self.automation.is_connected()

    def disconnect(self) -> None:
        self.automation.disconnect()

    def search_jobs_many(
        self,
        resume: ResumeProfile,
        variations: list[SearchVariation],
        limit_per_query: int,
    ) -> list[tuple[SearchVariation, list[dict]]]:
        if hasattr(self.automation, "search_jobs_many"):
            return self.automation.search_jobs_many(
                resume,
                variations,
                limit_per_query,
                collect_card_diagnostics=False,
            )
        results: list[tuple[SearchVariation, list[dict]]] = []
        for variation in variations:
            results.append(
                (
                    variation,
                    self.automation.search_jobs(
                        resume,
                        variation.location,
                        variation.query,
                        limit_per_query,
                        collect_card_diagnostics=False,
                    ),
                )
            )
        return results

    def apply_to_job(
        self,
        job: JobOpportunity,
        resume: ResumeProfile,
        candidate_profile: CandidateProfile | None,
        *,
        dry_run: bool,
        capture_evidence: bool,
        screening_context: dict | None = None,
        cancellation=None,
    ):
        return self.automation.apply_to_job(
            job,
            resume,
            candidate_profile,
            dry_run=dry_run,
            capture_evidence=capture_evidence,
            screening_context=screening_context,
            cancellation=cancellation,
        )
