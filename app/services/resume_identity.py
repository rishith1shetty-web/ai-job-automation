"""Resume identity via a content fingerprint, plus the fingerprint-aware queries used by
application history and successful-job exclusion.

The same resume re-uploaded later may receive a different ``resume_profiles.id``. To
recognise it as the same resume we hash the *parsed* resume text (already stored in
``ResumeProfile.raw_text``) rather than relying on filename, path, timestamp or id. A
resume that was actually edited produces different parsed text and therefore a new
fingerprint (a new identity), which is the intended behaviour.

This module is portal-agnostic: portal isolation is applied by the callers, which always
pass an explicit portal. Nothing here reparses a resume — it only reads stored text.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ApplicationRecord, ApplicationStatus, JobOpportunity, ResumeProfile
from app.services.application_outcome import SUCCESS_APPLICATION_STATUSES

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_resume_text(raw_text: str | None) -> str:
    """Canonicalise parsed resume text so cosmetic differences (encoding, line endings,
    repeated/trailing whitespace, case) do not change the fingerprint:

    NFKC Unicode normalisation → normalise line endings → collapse whitespace runs to a
    single space → trim → lowercase.
    """
    if not raw_text:
        return ""
    text = unicodedata.normalize("NFKC", raw_text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().lower()


def compute_resume_fingerprint(raw_text: str | None) -> str | None:
    """SHA-256 (hex) of the canonical resume text, or ``None`` when there is no usable
    content (so a legacy/empty resume is never assigned an invented shared fingerprint)."""
    canonical = normalize_resume_text(raw_text)
    if not canonical:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resume_fingerprint_family_ids(db: Session, resume: ResumeProfile) -> list[int]:
    """All ``resume_profiles.id`` values that share ``resume``'s fingerprint (its identity
    family, including itself). Falls back to just this resume's id when it has no
    fingerprint, so a legacy/unfingerprintable resume behaves exactly as before."""
    fingerprint = getattr(resume, "resume_fingerprint", None)
    if not fingerprint:
        return [resume.id]
    ids = db.scalars(
        select(ResumeProfile.id).where(ResumeProfile.resume_fingerprint == fingerprint)
    ).all()
    return sorted(set(ids) | {resume.id})


def resume_family_ids_for(db: Session, resume_id: int) -> list[int]:
    """Convenience wrapper: resolve the fingerprint family for a resume id. Returns just
    ``[resume_id]`` if the resume is missing or has no fingerprint."""
    resume = db.get(ResumeProfile, resume_id)
    if resume is None:
        return [resume_id]
    return resume_fingerprint_family_ids(db, resume)


def successful_jobs_for_fingerprint(
    db: Session,
    resume: ResumeProfile,
    portal: str,
    statuses: Iterable[ApplicationStatus] | None = None,
) -> list[JobOpportunity]:
    """Jobs that have been *successfully* completed for this resume's fingerprint family
    on the given portal. Portal-isolated: only jobs whose ``portal`` matches are returned,
    so a LinkedIn success never affects Dice results and vice versa.

    ``statuses`` narrows which application outcomes count as "successful". It defaults to
    SUCCESS_APPLICATION_STATUSES (live submit or submit-ready dry run) so existing callers
    keep their exact behaviour. Find Matches passes APPLIED only, because a submit-ready dry
    run must stay searchable -- only a confirmed live submission may hide a job from search.
    """
    family_ids = resume_fingerprint_family_ids(db, resume)
    statement = (
        select(JobOpportunity)
        .join(ApplicationRecord, ApplicationRecord.job_id == JobOpportunity.id)
        .where(JobOpportunity.resume_id.in_(family_ids))
        .where(JobOpportunity.portal == portal)
        .where(ApplicationRecord.status.in_(tuple(statuses or SUCCESS_APPLICATION_STATUSES)))
    )
    return list(db.scalars(statement).unique().all())


def backfill_resume_fingerprints(db: Session) -> dict:
    """Idempotently populate ``resume_fingerprint`` for existing rows from their already
    stored parsed text. Never reparses files, never touches rows that already have a
    fingerprint, and never invents a fingerprint for rows with no usable text.

    Returns a summary: ``{"updated", "already_set", "skipped_no_text", "skipped_ids"}``.

    This runs on every startup, so it must not scale with the number of resumes already
    fingerprinted. Only rows with a NULL fingerprint are hydrated (they are the only ones
    that can change); the already-complete rows are counted in SQL rather than loaded, so
    their ``raw_text`` never leaves the database. Once every eligible row is fingerprinted,
    the eligible-row query returns nothing and the whole backfill is a no-op.
    """
    already_set = (
        db.scalar(
            select(func.count())
            .select_from(ResumeProfile)
            .where(ResumeProfile.resume_fingerprint.is_not(None))
        )
        or 0
    )
    # Only NULL-fingerprint rows are candidates. Rows that already have one are never
    # touched, so an existing fingerprint can never be recomputed or overwritten.
    rows = list(
        db.scalars(
            select(ResumeProfile).where(ResumeProfile.resume_fingerprint.is_(None))
        ).all()
    )
    updated = 0
    skipped_no_text = 0
    skipped_ids: list[int] = []
    for resume in rows:
        fingerprint = compute_resume_fingerprint(resume.raw_text)
        if fingerprint is None:
            # No usable stored text — leave it NULL rather than invent an identity.
            skipped_no_text += 1
            skipped_ids.append(resume.id)
            continue
        resume.resume_fingerprint = fingerprint
        db.add(resume)
        updated += 1
    if updated:
        db.commit()
    return {
        "updated": updated,
        "already_set": already_set,
        "skipped_no_text": skipped_no_text,
        "skipped_ids": skipped_ids,
    }
