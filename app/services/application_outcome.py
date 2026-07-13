"""Centralized classification of application outcomes.

Single source of truth for "which application statuses count as a successful
completion". Used by:
  * successful-job exclusion in Find Matches (a completed job must not reappear), and
  * application-history / summary logic,
so the definition cannot drift between features.

A successful completion is either a live submission (``APPLIED``) or a dry run that
reached the submit-ready stage (``DRY_RUN_READY``). These are the exact terminal-success
members of :class:`ApplicationStatus`; no new status strings are invented. This mirrors
the React KPI mapping (``frontend/src/lib/applyOutcome.ts``), where ``applied`` and
``dry_run_ready`` are successes (``reached_review`` is the batch-level string the backend
derives from a ``DRY_RUN_READY`` record).

Queued / running / matched are NOT terminal and never count as success. Unsuccessful
terminal outcomes (failed, skipped, needs_review, unsupported_form, timeouts, …) remain
eligible for retry and are deliberately excluded from this set.
"""

from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApplicationRecord, ApplicationStatus

# Terminal statuses that mark a job as successfully completed for a resume + portal.
SUCCESS_APPLICATION_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {ApplicationStatus.APPLIED, ApplicationStatus.DRY_RUN_READY}
)

# Steps a dry run reports when it reached the submit/review stage. Historically LinkedIn
# returned SKIPPED for these, and the batch view compensated by re-reading current_step.
# The status itself is now DRY_RUN_READY; this set stays as the single definition used by
# both the batch view (for records written before the fix) and the backfill below.
DRY_RUN_COMPLETE_STEPS: frozenset[str] = frozenset(
    {"dry_run_complete", "review_reached", "ready_to_submit"}
)


def is_successful_application(status: ApplicationStatus | str | None) -> bool:
    """True when ``status`` represents a successful completion (live submit or a
    submit-ready dry run). Accepts the enum or its string value."""
    if status is None:
        return False
    if isinstance(status, ApplicationStatus):
        return status in SUCCESS_APPLICATION_STATUSES
    return any(status == member.value for member in SUCCESS_APPLICATION_STATUSES)


# Outcome precedence. Once a job has successfully completed for a resume + portal, that
# success is authoritative and a later failed/skipped/needs-review attempt must never
# erase it. Higher rank wins:
#
#     APPLIED (3) > DRY_RUN_READY (2) > every unsuccessful / non-terminal status (0)
#
# Every status not listed here -- FAILED, SKIPPED, NEEDS_REVIEW, UNSUPPORTED_FORM, QUEUED,
# MATCHED -- ranks 0, so they neither outrank nor overwrite a recorded success. This is the
# single definition of status ranking; do not re-derive it elsewhere.
_STATUS_PRECEDENCE: dict[ApplicationStatus, int] = {
    ApplicationStatus.APPLIED: 3,
    ApplicationStatus.DRY_RUN_READY: 2,
}
UNSUCCESSFUL_PRECEDENCE = 0


def application_status_precedence(status: ApplicationStatus | None) -> int:
    """Rank of ``status`` for outcome precedence (see ``_STATUS_PRECEDENCE``)."""
    if status is None:
        return UNSUCCESSFUL_PRECEDENCE
    return _STATUS_PRECEDENCE.get(status, UNSUCCESSFUL_PRECEDENCE)


# Columns of ApplicationRecord that describe ONE attempt and must never leak from a
# previous attempt into a later one. Mapped to a factory producing the neutral value for
# that column's type, so no mutable default is ever shared between rows:
#
#   nullable String/Text columns -> None
#   JSONB list columns           -> a fresh []
#
# Everything else on the row is identity/audit (id, job_id, portal, status, dry_run,
# application_date, attempt_count, the job relationship) and is deliberately NOT reset.
# Note there is no separate screening column: screening decisions are carried inside
# `run_log` / `evidence_sources`, which are both reset here.
_ATTEMPT_FIELD_FACTORIES: dict[str, Callable[[], object]] = {
    "current_step": lambda: None,
    "evidence_path": lambda: None,
    "resume_filename_used": lambda: None,
    "resume_upload_status": lambda: None,
    "resume_upload_evidence_path": lambda: None,
    "reasoning_type": lambda: None,
    "reasoning_summary": lambda: None,
    "confidence_score": lambda: None,
    "evidence_sources": list,
    "error_message": lambda: None,
    "run_log": list,
}

ATTEMPT_SPECIFIC_FIELDS: frozenset[str] = frozenset(_ATTEMPT_FIELD_FACTORIES)


def reset_attempt_specific_fields(record, keep: Iterable[str] = ()) -> None:
    """Clear every attempt-specific column on ``record`` back to its neutral value, except
    those named in ``keep`` (the columns the incoming attempt is about to write).

    Used when a new attempt supersedes the stored one: without it, a field the new attempt
    did not produce would silently retain the *previous* attempt's value -- e.g. a browser
    timeout inheriting the earlier run's evidence_path, reasoning and resume-upload status,
    which reads as though the failed attempt owned that evidence.
    """
    kept = set(keep)
    for field, factory in _ATTEMPT_FIELD_FACTORIES.items():
        if field in kept:
            continue
        setattr(record, field, factory())


def outcome_supersedes(
    incoming: ApplicationStatus | None, existing: ApplicationStatus | None
) -> bool:
    """True when an ``incoming`` attempt's outcome may overwrite the ``existing`` record's.

    An outcome supersedes another only when it ranks at least as high:

      * FAILED after DRY_RUN_READY  -> False (the success stands)
      * SKIPPED after APPLIED       -> False
      * DRY_RUN_READY after APPLIED -> False (a dry run never demotes a live submit)
      * DRY_RUN_READY after FAILED  -> True  (upgrade)
      * APPLIED after DRY_RUN_READY -> True  (upgrade: the live submit went through)
      * FAILED after SKIPPED        -> True  (both rank 0: unsuccessful rows still refresh)
    """
    return application_status_precedence(incoming) >= application_status_precedence(existing)


def backfill_dry_run_ready_records(db: Session) -> int:
    """Promote historical successful dry runs from SKIPPED to DRY_RUN_READY.

    Before the fix, a LinkedIn dry run that reached the submit/review stage was persisted
    as SKIPPED with ``current_step="dry_run_complete"`` -- a successful run stored under a
    failure status, so it never appeared in Application History. These are exactly the
    records the batch view already reported as "reached_review", so re-labelling them is a
    correction, not a reinterpretation.

    Non-destructive and idempotent: only rewrites ``status`` on rows that are SKIPPED *and*
    carry a dry-run-complete step. Deletes nothing, touches no other column, and re-running
    is a no-op. Returns the number of rows corrected.
    """
    rows = list(
        db.scalars(
            select(ApplicationRecord)
            .where(ApplicationRecord.status == ApplicationStatus.SKIPPED)
            .where(ApplicationRecord.current_step.in_(DRY_RUN_COMPLETE_STEPS))
        ).all()
    )
    for record in rows:
        record.status = ApplicationStatus.DRY_RUN_READY
        db.add(record)
    if rows:
        db.commit()
    return len(rows)
